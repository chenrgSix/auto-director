from copy import deepcopy

import httpx
import pytest

from app.agents.directing import generation_budget
from app.core.errors import AppError
from app.generation.parameters import resolve_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_rendered_story_rebind import fail_after_frames
from tests.test_workflow_duration import install_cloud


def low_vram(comfy):
    original = comfy.handle

    def handle(request):
        result = original(request)
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"vram_free": 6 * 1024**3}]})
        return result

    comfy.handle = handle


@pytest.mark.parametrize("duration", [60, 90])
@pytest.mark.parametrize("quality", ["fast", "standard", "high"])
def test_eight_gb_gpu_plans_configured_five_second_shots_in_every_quality(
    system, duration, quality
):
    client, app, comfy = system
    low_vram(comfy)
    episode = preview(system, target_duration=duration, max_shot_duration=None, quality=quality)
    assert episode["budget"]["low_memory"]
    assert episode["preview"]["max_duration"] == 5
    assert [s["duration"] for s in episode["shots"]] == [5] * (duration // 5)
    assert not comfy.prompts and not app.state.store.list("job", episode["id"])
    assert (
        client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=edits(episode)).status_code
        == 200
    )


@pytest.mark.parametrize("binding", ["frames", "seconds"])
def test_five_second_local_video_reaches_final_json_on_low_memory_gpu(system, binding):
    client, app, comfy = system
    low_vram(comfy)
    wid = install_cloud(client, comfy, remote=False) if binding == "seconds" else "default_video"
    episode = preview(
        system, target_duration=5, max_shot_duration=None, video_workflow_id=wid, quality="fast"
    )
    assert len(episode["shots"]) == 1 and episode["shots"][0]["duration"] == 5
    assert episode["preview"]["remote_video"] is False
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/approve",
            json={"expected_version": episode["version"]},
        ).status_code
        == 202
    )
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["final_duration"] == pytest.approx(5, abs=0.1)
    job = next(j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SHOT_VIDEO")
    role = job["profile_snapshot"]["bindings"]["duration"]
    assert job["patched_workflow"][role["node_id"]]["inputs"][role["input"]] == (
        81 if binding == "frames" else 5
    )
    assert job["input_values"]["timeline_duration"] == 5
    assert job["budget_snapshot"]["max_duration"] == 5


def test_failed_legacy_three_second_budget_resumes_five_seconds_without_rebuilding_images(
    system, monkeypatch
):
    client, app, comfy = system
    low_vram(comfy)
    original = fail_after_frames(system, monkeypatch, target_duration=5, max_shot_duration=5)
    original = app.state.store.update(
        "episode",
        original["id"],
        {
            "budget": {**original["budget"], "max_duration": 3, "render_max_duration": 3},
            "refresh_workflow_budget": False,
        },
    )
    jobs = app.state.store.list("job", original["id"])
    old_ids = {j["id"] for j in jobs}
    assert client.post(f"/api/v1/episodes/{original['id']}/generate").status_code == 202
    final = wait_episode(client, original["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["plan"] == original["plan"] and final["bible"] == original["bible"]
    assert final["references"] == original["references"]
    assert final["shots"][0]["start_frame_asset_id"] == original["shots"][0]["start_frame_asset_id"]
    assert final["shots"][0]["end_frame_asset_id"] == original["shots"][0]["end_frame_asset_id"]
    assert final["shots"][0]["duration"] == 5 and final["budget"]["max_duration"] == 5
    assert final["metrics"]["llm_calls"] == original["metrics"]["llm_calls"]
    new = [j for j in app.state.store.list("job", original["id"]) if j["id"] not in old_ids]
    assert len(new) == 1 and new[0]["type"] == "SHOT_VIDEO"
    for job in jobs:
        assert app.state.store.get("job", job["id"]) == job


def test_legacy_preview_refreshes_only_timing_limits_without_rewriting_saved_story(system):
    client, app, comfy = system
    low_vram(comfy)
    episode = preview(system, target_duration=5, max_shot_duration=None)

    def old_limits(current):
        current["budget"].update(max_duration=3, render_max_duration=3)
        current["preview"].update(max_duration=3, render_max_duration=3)

    original = app.state.store.update("episode", episode["id"], old_limits)
    viewed = client.get(f"/api/v1/episodes/{episode['id']}").json()
    assert viewed["preview"]["max_duration"] == 5 and viewed["budget"]["max_duration"] == 5
    assert app.state.store.get("episode", episode["id"]) == original
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=edits(viewed))
    assert response.status_code == 200, response.text
    saved = response.json()
    for field in ("plan", "bible", "references", "metrics"):
        assert saved[field] == original[field]
    expected_shots = deepcopy(original["shots"])
    for shot in expected_shots:
        shot.setdefault("preview_edited_fields", [])
    assert saved["shots"] == expected_shots
    assert saved["budget"]["max_duration"] == 5
    assert saved["budget"]["width"] == original["budget"]["width"]
    assert not comfy.prompts


def test_explicit_shorter_limit_and_model_cap_still_reject_excess_duration(system):
    _, app, _ = system
    profile = app.state.store.get("workflow", "default_video")
    budget = generation_budget(
        {"quality": "fast", "aspect_ratio": "9:16", "max_shot_duration": 2},
        profile["capabilities"],
        {"devices": [{"vram_free": 6 * 1024**3}]},
    )
    with pytest.raises(AppError, match="上限"):
        resolve_parameters(profile, {"duration": 3}, {}, {}, False, budget)
    assert (
        resolve_parameters(profile, {"duration": 2}, {}, {}, False, budget)[0]["timeline_duration"]
        == 2
    )
    smaller = deepcopy(profile)
    smaller["capabilities"]["max_duration"] = 1
    with pytest.raises(AppError, match="上限"):
        resolve_parameters(smaller, {"duration": 2}, {}, {}, False, budget)
