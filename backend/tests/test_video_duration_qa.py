import asyncio

import pytest

from app.core.errors import AppError
from app.media.service import inspect_media, run_process, video_duration
from tests.test_api_pipeline import wait_episode
from tests.test_episode_rerun import complete


def short_video(tmp_path):
    path = tmp_path / "short.mp4"
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
            "1",
            "-c:v",
            "libx264",
            str(path),
        )
    )
    return path


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_video_duration_is_rejected(duration):
    with pytest.raises(AppError) as caught:
        video_duration({"duration": duration, "video": {"duration": str(duration)}})
    assert caught.value.code == "INVALID_MEDIA"


def test_long_audio_does_not_replace_video_length():
    assert (
        video_duration({"duration": 5, "video": {"duration": "1"}, "audio": {"duration": "5"}}) == 1
    )


@pytest.mark.parametrize("retries", [0, 1])
def test_short_valid_outputs_complete_without_duration_retries(system, tmp_path, retries):
    client, app, comfy = system
    comfy.video_bytes = short_video(tmp_path).read_bytes()
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "short output",
            "target_duration": 10,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
            "max_retries": retries,
        },
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    result = wait_episode(client, id)
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["final_duration"] == pytest.approx(2, abs=0.1)
    assert all(s["status"] == "PASSED" and s["actual_duration"] == 1 for s in result["shots"])
    assert [s["duration"] for s in result["shots"]] == [5, 5]
    jobs = app.state.store.list("job", id)
    assert len([j for j in jobs if j["type"] == "SHOT_VIDEO"]) == 2
    assert all(j["status"] == "COMPLETED" for j in jobs)


def test_continue_preserves_legacy_short_video_and_continuous_successor(system, tmp_path):
    client, app, comfy = system
    old = complete(system, target_duration=10)
    id = old["id"]
    video_id = old["shots"][0]["video_asset_id"]
    path = short_video(tmp_path)
    app.state.assets.path(video_id).write_bytes(path.read_bytes())
    app.state.store.update("asset", video_id, {"metadata": asyncio.run(inspect_media(path))})
    app.state.store.update("episode", id, {"status": "FAILED"})
    before = len(comfy.prompts)
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    result = wait_episode(client, id)
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["plan"] == old["plan"] and result["references"] == old["references"]
    assert result["shots"][0]["start_frame_asset_id"] == old["shots"][0]["start_frame_asset_id"]
    assert result["shots"][0]["end_frame_asset_id"] == old["shots"][0]["end_frame_asset_id"]
    assert [s["video_asset_id"] for s in result["shots"]] == [
        s["video_asset_id"] for s in old["shots"]
    ]
    assert len(comfy.prompts) == before
    assert result["rerun_history"][-1]["scope"] == "compose"
    assert client.get(f"/api/v1/assets/{video_id}/file").content == path.read_bytes()
    assert client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").status_code == 200


def test_completed_job_cache_accepts_short_valid_video(system):
    _, app, _ = system
    episode = complete(system, target_duration=5)
    job = next(j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SHOT_VIDEO")
    asset = app.state.store.get("asset", job["output_asset_ids"][0])
    metadata = {**asset["metadata"], "duration": 1}
    app.state.store.update("asset", asset["id"], {"metadata": metadata})
    assert asyncio.run(app.state.engine.run(job["id"]))[0]["id"] == asset["id"]
    assert app.state.store.get("job", job["id"])["status"] == "COMPLETED"
