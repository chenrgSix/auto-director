import asyncio

import pytest

from app.core.errors import AppError
from app.media.service import inspect_media, require_video_duration, run_process
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


@pytest.mark.parametrize("fps", [16, 24])
def test_duration_tolerance_is_one_frame(fps):
    require_video_duration(
        {"duration": 5 - 1 / fps, "video": {"duration": str(5 - 1 / fps)}}, 5, fps
    )
    with pytest.raises(AppError, match="短于"):
        require_video_duration(
            {"duration": 5 - 2 / fps, "video": {"duration": str(5 - 2 / fps)}}, 5, fps
        )


def test_long_audio_cannot_hide_short_video():
    with pytest.raises(AppError) as caught:
        require_video_duration(
            {"duration": 5, "video": {"duration": "1"}, "audio": {"duration": "5"}}, 5, 16
        )
    assert caught.value.code == "VIDEO_TOO_SHORT"
    assert caught.value.details["actual"] == 1


@pytest.mark.parametrize("retries", [0, 1])
def test_short_outputs_fail_locally_without_generating_later_shots(system, tmp_path, retries):
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
    failed = wait_episode(client, id)
    assert failed["error"]["code"] == "VIDEO_TOO_SHORT"
    assert failed["shots"][0]["status"] == "FAILED"
    assert failed["shots"][1]["status"] == "PENDING"
    assert not failed["shots"][0]["actual_end_frame_asset_id"]
    jobs = app.state.store.list("job", id)
    video_jobs = [j for j in jobs if j["type"] == "SHOT_VIDEO"]
    assert len(video_jobs) == retries + 1
    assert all(j["status"] == "FAILED" and j["output_asset_ids"] for j in video_jobs)
    assert len([j for j in jobs if j["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"}]) == 2
    for job in video_jobs:
        assert client.get(f"/api/v1/assets/{job['output_asset_ids'][0]}/file").status_code == 200


def test_continue_repairs_legacy_short_video_and_continuous_successor(system, tmp_path):
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
    assert all(
        new["video_asset_id"] != prior["video_asset_id"]
        for new, prior in zip(result["shots"], old["shots"], strict=True)
    )
    assert (
        result["shots"][1]["start_frame_asset_id"]
        == result["shots"][0]["actual_end_frame_asset_id"]
    )
    assert len(comfy.prompts) - before == 3  # Two videos and the dependent end frame.
    history = result["rerun_history"][-1]
    assert history["reason"] == "VIDEO_TOO_SHORT" and history["shots"] == old["shots"]
    assert client.get(f"/api/v1/assets/{video_id}/file").content == path.read_bytes()
    assert client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").status_code == 200


def test_completed_job_cache_rechecks_legacy_video_duration(system):
    _, app, _ = system
    episode = complete(system, target_duration=5)
    job = next(j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SHOT_VIDEO")
    asset = app.state.store.get("asset", job["output_asset_ids"][0])
    metadata = {**asset["metadata"], "duration": 1}
    app.state.store.update("asset", asset["id"], {"metadata": metadata})
    with pytest.raises(AppError) as caught:
        asyncio.run(app.state.engine.run(job["id"]))
    assert caught.value.code == "VIDEO_TOO_SHORT"
    assert app.state.store.get("job", job["id"])["status"] == "FAILED"
