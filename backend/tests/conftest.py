import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.media.service import run_process
from tests.fakes import FakeComfy, FakeProvider


@pytest.fixture
def system(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required for pipeline acceptance")
    video = tmp_path / "fixture.mp4"
    asyncio.run(
        run_process(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x256:rate=16",
            "-t",
            "5.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        )
    )
    config = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_model="fixture",
        vlm_model="fixture-vision",
        poll_interval=0.01,
    )
    comfy = FakeComfy(config, video.read_bytes())
    app = create_app(config, client_factory=comfy.client, provider_factory=FakeProvider)
    with TestClient(app) as client:
        yield client, app, comfy
