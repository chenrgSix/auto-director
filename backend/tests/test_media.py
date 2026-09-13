import array
import shutil
import sys

import pytest
from PIL import Image

from app.core.errors import AppError
from app.media.service import compose, extract_frame, inspect_media, probe, run_process

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
    result = await compose([(silent, 1), (audio, 1)], output, 256, 256, 16)
    assert abs(result["duration"] - 2) < 0.2
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


async def test_continuity_tail_uses_exported_duration_not_unused_video_frames(tmp_path):
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
    exported_tail = await extract_frame(clip, tmp_path / "exported.png", duration_limit=1)
    original_tail = await extract_frame(clip, tmp_path / "original.png")
    with Image.open(exported_tail) as frame:
        red, _, blue = frame.convert("RGB").getpixel((32, 32))
        assert red > 200 and blue < 20
    with Image.open(original_tail) as frame:
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
        "5.2",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(source),
    )
    output = tmp_path / "long.mp4"
    total = clip_duration * 120
    result = await compose([(source, clip_duration)] * 120, output, 64, 64, 16)
    assert abs(result["duration"] - total) < 0.1
    assert int(result["video"]["nb_frames"]) == round(total * 16)
    assert abs(float(result["audio"]["duration"]) - total) < 0.1
    frame = await extract_frame(output, tmp_path / "last.png")
    assert (await inspect_media(frame))["kind"] == "image"
