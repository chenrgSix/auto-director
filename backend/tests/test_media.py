import array
import shutil
import sys

import pytest
from PIL import Image

from app.core.errors import AppError
from app.media.service import (
    compose,
    extract_frame,
    inspect_media,
    media_duration,
    probe,
    run_process,
)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="real FFmpeg required"
)


async def test_real_ffmpeg_normalizes_silent_and_audio_clips(tmp_path):
    silent, audio = tmp_path / "silent.mp4", tmp_path / "audio.mp4"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=16",
        "-t",
        "1.1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(silent),
    )
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=180x320:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "1.1",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-pix_fmt",
        "yuv420p",
        str(audio),
    )
    output = tmp_path / "final.mp4"
    result = await compose([silent, audio], output, 256, 256, 16)
    assert result["duration"] == pytest.approx(
        media_duration(await probe(silent)) + media_duration(await probe(audio)), abs=0.07
    )
    assert result["video"]["width"] == 256
    assert result["audio"]["sample_rate"] == "48000"
    # Detect discarded source audio or sound shifted into the silent first shot.
    raw = await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(output),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "f32le",
        "pipe:1",
    )
    samples = array.array("f", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    silent_window, tone_window = samples[1600:6400], samples[9600:14400]
    assert len(silent_window) == len(tone_window) == 4800
    assert max(abs(value) for value in silent_window) < 0.001
    assert sum(value * value for value in tone_window) / len(tone_window) > 0.001
    frame = await extract_frame(output, tmp_path / "last.png")
    assert (await inspect_media(frame))["kind"] == "image"
    assert (await probe(output))["video"]["codec_name"] == "h264"


async def test_fake_extension_is_rejected(tmp_path):
    invalid = tmp_path / "image.png"
    invalid.write_text("not an image")
    with pytest.raises(AppError):
        await inspect_media(invalid)


async def test_full_composition_and_continuity_preserve_the_blue_tail(tmp_path):
    clip = tmp_path / "red-then-blue.mp4"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=red:size=64x64:rate=16:duration=1",
        "-f",
        "lavfi",
        "-i",
        "color=blue:size=64x64:rate=16:duration=1",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    )
    output = tmp_path / "full.mp4"
    result = await compose([clip], output, 64, 64, 16)
    assert result["duration"] == pytest.approx(2, abs=0.05)
    for name, path in (("source", clip), ("final", output)):
        tail = await extract_frame(path, tmp_path / f"{name}-tail.png")
        with Image.open(tail) as frame:
            red, _, blue = frame.convert("RGB").getpixel((32, 32))
            assert blue > 200 and red < 20


@pytest.mark.parametrize("clip_duration", [5, 4.99])
async def test_long_composition_does_not_accumulate_frame_or_audio_padding(tmp_path, clip_duration):
    source = tmp_path / "source.mp4"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=16",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(clip_duration),
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(source),
    )
    output = tmp_path / "long.mp4"
    source_metadata = await probe(source)
    total = media_duration(source_metadata) * 120
    result = await compose([source] * 120, output, 64, 64, 16)
    assert abs(result["duration"] - total) < 0.1
    assert int(result["video"]["nb_frames"]) == int(source_metadata["video"]["nb_frames"]) * 120
    assert abs(float(result["audio"]["duration"]) - total) < 0.1
    frame = await extract_frame(output, tmp_path / "last.png")
    assert (await inspect_media(frame))["kind"] == "image"


async def test_full_composition_keeps_audio_after_video_and_silent_next_clip(tmp_path):
    source, silent = tmp_path / "voice.mov", tmp_path / "silent.mp4"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=blue:size=64x64:rate=16:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=48000:duration=2",
        "-c:v",
        "libx264",
        "-c:a",
        "pcm_s16le",
        str(source),
    )
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=red:size=64x64:rate=16:duration=1",
        "-c:v",
        "libx264",
        str(silent),
    )
    output = tmp_path / "complete.mp4"
    result = await compose([source, silent], output, 64, 64, 16)
    assert result["duration"] == pytest.approx(3, abs=0.05)
    assert float(result["video"]["duration"]) == pytest.approx(3, abs=0.05)
    raw = await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(output),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "f32le",
        "pipe:1",
    )
    samples = array.array("f", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    assert sum(v * v for v in samples[12000:15200]) / 3200 > 0.001
    assert max(abs(v) for v in samples[17600:22400]) < 0.001
    held = await extract_frame(output, tmp_path / "held.png", 0.5)
    tail = await extract_frame(output, tmp_path / "tail.png")
    with Image.open(held) as frame:
        red, _, blue = frame.convert("RGB").getpixel((32, 32))
        assert blue > 200 and red < 20
    with Image.open(tail) as frame:
        red, _, blue = frame.convert("RGB").getpixel((32, 32))
        assert red > 200 and blue < 20
