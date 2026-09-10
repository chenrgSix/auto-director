import asyncio
import shutil
import time

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


def wait_episode(client, id):
    for _ in range(600):
        episode = client.get(f"/api/v1/episodes/{id}").json()
        if episode["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return episode
        time.sleep(0.02)
    raise AssertionError("pipeline timeout")


def test_api_full_generation_and_targeted_video_retry(system):
    client, app, comfy = system
    assert client.get("/api/v1/health").status_code == 200
    assert client.post("/api/v1/comfyui/test").status_code == 200
    result = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Three lions in the Jurassic",
            "target_duration": 5,
            "max_shot_duration": 3,
            "width": 256,
            "height": 256,
        },
    )
    assert result.status_code == 201, result.text
    id = result.json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode.get("error")
    assert abs(episode["final_duration"] - 5) < 0.5
    assert len(episode["shots"]) == 2
    assert len(episode["references"]) == 3
    assert (
        episode["shots"][1]["start_frame_asset_id"]
        == episode["shots"][0]["actual_end_frame_asset_id"]
    )
    final = client.get(f"/api/v1/assets/{episode['final_video_asset_id']}/file")
    assert final.status_code == 200 and final.headers["content-type"] == "video/mp4"
    shot = episode["shots"][1]
    before = len(
        [p for p in comfy.prompts.values() if p["prompt"]["save"]["class_type"] == "SaveImage"]
    )
    response = client.post(f"/api/v1/shots/{shot['id']}/retry-video")
    assert response.status_code == 202, response.text
    retried = wait_episode(client, id)
    assert retried["status"] == "COMPLETED", retried.get("error")
    assert retried["shots"][1]["start_frame_asset_id"] == shot["start_frame_asset_id"]
    assert retried["shots"][1]["end_frame_asset_id"] == shot["end_frame_asset_id"]
    assert before == len(
        [p for p in comfy.prompts.values() if p["prompt"]["save"]["class_type"] == "SaveImage"]
    )
    assert retried["shots"][0]["video_asset_id"] == episode["shots"][0]["video_asset_id"]
    timeline = [{"id": s["id"], "enabled": True} for s in reversed(retried["shots"])]
    changed = client.patch(f"/api/v1/episodes/{id}/timeline", json={"shots": timeline}).json()
    assert changed["final_video_asset_id"] is None
    assert changed["shots"][0]["status"] == "STALE"


def test_input_errors_secrets_and_cross_origin_protection(system):
    client, _, _ = system
    settings = client.get("/api/v1/settings").json()
    assert "llm_api_key" not in settings
    for payload in [
        {"idea": "  ", "target_duration": 5},
        {"idea": "x", "target_duration": 31},
        {"idea": "x", "target_duration": 5, "width": 257, "height": 256},
    ]:
        assert client.post("/api/v1/episodes", json=payload).status_code == 422
    assert (
        client.post(
            "/api/v1/episodes",
            json={"idea": "x", "target_duration": 5},
            headers={"Origin": "https://malicious.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/workflows/import",
            json={"name": "wrong", "type": "image", "workflow": {"nodes": []}},
        ).status_code
        == 400
    )
    assert client.get("/api/v1/assets/not-existing/file").status_code == 404
    assert (
        client.post(
            "/api/v1/assets", files={"file": ("fake.png", b"not an image", "image/png")}
        ).status_code
        == 400
    )


def test_workflow_test_run_upload_and_default_protection(system):
    client, _, comfy = system
    upload = client.post(
        "/api/v1/assets", files={"file": ("frame.png", comfy.image_bytes, "image/png")}
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()["id"]
    result = client.post(
        "/api/v1/workflows/default_video/test-run",
        json={
            "values": {"prompt": "test", "duration": 2, "fps": 16},
            "asset_bindings": {"start_frame": asset, "end_frame": asset},
        },
    )
    assert result.status_code == 202, result.text
    for _ in range(300):
        job = client.get(f"/api/v1/jobs/{result.json()['id']}").json()
        if job["status"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert job["status"] == "COMPLETED", job.get("error")
    assert client.delete("/api/v1/workflows/default_video").status_code == 409
