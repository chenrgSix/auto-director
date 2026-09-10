import asyncio
import shutil
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.schemas import EpisodePlan, QAResult
from app.core.config import Settings
from app.db.store import Store
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


@pytest.mark.parametrize("duration", [10, 15])
def test_longer_episode_exports_requested_duration(system, duration):
    client, _, _ = system
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "duration fixture", "target_duration": duration, "width": 256, "height": 256},
    ).json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode.get("error")
    assert abs(episode["final_duration"] - duration) <= 0.5
    assert len(episode["shots"]) == duration // 5
    assert all(shot["status"] == "PASSED" for shot in episode["shots"])


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


def test_cancel_blocks_readmission_until_old_task_has_exited(system):
    client, app, _ = system

    class SlowProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if schema is EpisodePlan:
                await asyncio.sleep(0.35)
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = SlowProvider
    id = client.post(
        "/api/v1/episodes", json={"idea": "cancel fixture", "target_duration": 5}
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    for _ in range(50):
        if client.get(f"/api/v1/episodes/{id}").json()["status"] == "PLANNING":
            break
        time.sleep(0.005)
    assert client.post(f"/api/v1/episodes/{id}/cancel").json()["status"] == "CANCELLED"
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    assert client.delete(f"/api/v1/episodes/{id}").status_code == 409
    time.sleep(0.4)
    result = client.get(f"/api/v1/episodes/{id}").json()
    assert result["status"] == "CANCELLED" and not result["shots"]
    assert client.get(f"/api/v1/jobs?episode_id={id}").json() == []


def test_restart_marks_active_work_unknown_and_preserves_artifacts(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path)
    store = Store(tmp_path)
    episode = store.create(
        "episode",
        {"status": "RENDERING_VIDEO", "shots": [{"id": "kept", "video_asset_id": "asset-kept"}]},
    )
    job = store.create(
        "job",
        {"status": "RUNNING", "type": "SHOT_VIDEO", "comfy_prompt_id": "known-prompt"},
        parent=episode["id"],
    )
    store.close()
    with TestClient(create_app(config)) as client:
        result = client.get(f"/api/v1/episodes/{episode['id']}").json()
        assert result["status"] == "FAILED"
        assert result["shots"][0]["video_asset_id"] == "asset-kept"
        result = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert result["status"] == "UNKNOWN"
        assert result["comfy_prompt_id"] == "known-prompt"


@pytest.mark.parametrize(
    ("retries", "expected", "alternative"),
    [(1, "FAILED", False), (2, "COMPLETED", False), (2, "COMPLETED", True)],
)
def test_oom_fallback_obeys_budget_and_preserves_timeline(system, retries, expected, alternative):
    client, _, comfy = system
    if alternative:
        profile = client.get("/api/v1/workflows/default_video").json()
        graph = profile["workflow"]
        graph["low_sampler"] = graph.pop("sampler")
        for node in graph.values():
            for value in node["inputs"].values():
                if isinstance(value, list) and value[0] == "sampler":
                    value[0] = "low_sampler"
        low = client.post(
            "/api/v1/workflows/import",
            json={"name": "Low memory", "type": "video", "workflow": graph},
        )
        assert low.status_code == 201, low.text
        caps = {**profile["capabilities"], "low_memory_workflow_id": low.json()["id"]}
        assert (
            client.patch("/api/v1/workflows/default_video", json={"capabilities": caps}).status_code
            == 200
        )
    original = comfy.handle
    failing = set()
    videos = []

    def with_oom(request):
        result = original(request)
        if request.url.path == "/prompt":
            prompt_id = result.json()["prompt_id"]
            if comfy.prompts[prompt_id]["prompt"]["save"]["class_type"] == "SaveVideo":
                videos.append(prompt_id)
                if len(videos) <= 2:
                    failing.add(prompt_id)
        if (
            request.url.path.startswith("/history/")
            and request.url.path.rsplit("/", 1)[1] in failing
        ):
            prompt_id = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    prompt_id: {
                        "status": {
                            "completed": False,
                            "status_str": "error",
                            "messages": [
                                ["execution_error", {"exception_type": "OutOfMemoryError"}]
                            ],
                        }
                    }
                },
            )
        return result

    comfy.handle = with_oom
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "OOM fixture",
            "target_duration": 5,
            "width": 256,
            "height": 256,
            "qa_enabled": False,
            "max_retries": retries,
            "video_parameters": {"sampler.steps": 7},
        },
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == expected, episode.get("error")
    if expected == "FAILED":
        assert len(videos) == 2
        assert episode["shots"][0]["start_frame_asset_id"]
        assert episode["shots"][0]["end_frame_asset_id"]
    else:
        assert len(videos) == 5  # two failed full clips, three successful shorter segments
        assert abs(episode["final_duration"] - 5) < 0.5
        jobs = client.get(f"/api/v1/jobs?episode_id={id}").json()
        segments = sorted(
            [job for job in jobs if job["type"] == "VIDEO_SEGMENT"],
            key=lambda job: job["step_key"],
        )
        tail_id = segments[1]["asset_bindings"]["start_frame"]
        asset = client.get(f"/api/v1/assets?episode_id={id}").json()
        assert next(item for item in asset if item["id"] == tail_id)["type"] == "SEGMENT_END_FRAME"
        if alternative:
            assert all(job["parameter_values"] == {} for job in segments)


def test_action_qa_retries_only_video_and_keeps_immutable_qa_records(system):
    client, app, comfy = system

    class FailOnceProvider(FakeProvider):
        failed = False

        async def generate_json(self, system, context, schema, *, images=None):
            result = await super().generate_json(system, context, schema, images=images)
            if schema is QAResult and context["stage"] == "video" and not self.failed:
                self.failed = True
                result.action_accuracy = 0.3
            return result

    app.state.generation.provider_factory = FailOnceProvider
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "QA fixture", "target_duration": 5, "width": 256, "height": 256},
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode.get("error")
    assert (
        sum(p["prompt"]["save"]["class_type"] == "SaveImage" for p in comfy.prompts.values()) == 5
    )
    assert (
        sum(p["prompt"]["save"]["class_type"] == "SaveVideo" for p in comfy.prompts.values()) == 2
    )
    history = client.get(f"/api/v1/episodes/{id}/qa").json()
    assert len(history) == 3
    assert any(result["result"]["action_accuracy"] == 0.3 for result in history)
    assert client.delete(f"/api/v1/episodes/{id}").status_code == 204
    assert app.state.store.list("qa", id) == []
