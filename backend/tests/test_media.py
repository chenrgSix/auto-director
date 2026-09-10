import shutil

import pytest

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
    frame = await extract_frame(output, tmp_path / "last.png")
    assert (await inspect_media(frame))["kind"] == "image"
    assert (await probe(output))["video"]["codec_name"] == "h264"


async def test_fake_extension_is_rejected(tmp_path):
    invalid = tmp_path / "image.png"
    invalid.write_text("not an image")
    with pytest.raises(AppError):
        await inspect_media(invalid)
