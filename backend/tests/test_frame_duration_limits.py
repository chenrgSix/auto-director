from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import patch, refresh_profile
from app.workflows.duration import render_maximum
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import preview


def configure(system, definition=None):
    client, app, comfy = system
    app.state.store.update(
        "workflow", "default_video", lambda p: p["workflow"]["frames"]["inputs"].update(length=49)
    )
    comfy.info["WanFirstLastFrameToVideo"]["input"]["required"]["length"] = definition or [
        "INT",
        {"min": 1, "max": 49, "step": 4},
    ]
    return refresh_profile(app.state.store.get("workflow", "default_video"), comfy.info)


@pytest.mark.parametrize("total", [10, 60, 90])
def test_frame_maximum_limits_planning_before_assets(system, total):
    client, app, comfy = system
    configure(system)
    episode = preview(system, target_duration=total, max_shot_duration=5)
    assert episode["budget"]["max_duration"] == 3
    assert all(s["duration"] <= 3 for s in episode["shots"])
    assert sum(s["duration"] for s in episode["shots"]) == pytest.approx(total)
    assert not comfy.prompts and not app.state.store.list("asset", episode["id"])
    if total == 10:
        assert (
            client.post(
                f"/api/v1/episodes/{episode['id']}/approve",
                json={"expected_version": episode["version"]},
            ).status_code
            == 202
        )
        final = wait_episode(client, episode["id"])
        assert final["status"] == "COMPLETED", final.get("error")
        assert final["final_duration"] == pytest.approx(10, abs=0.1)


@pytest.mark.parametrize("fps,maximum", [(8, 5), (16, 3), (24, 2)])
def test_frame_limit_uses_selected_fps(system, fps, maximum):
    profile = configure(system)
    assert render_maximum(profile, {"fps": fps}) == maximum
    values, _, raw, _ = resolve_parameters(
        profile, {"duration": maximum, "fps": fps}, {}, {}, False
    )
    assert patch(profile, values, raw)["frames"]["inputs"]["length"] <= 49


def test_frame_minimum_and_enum_round_render_up_without_changing_timeline(system):
    profile = configure(system, [[49, 81], {}])
    original = deepcopy(profile)
    values, _, raw, _ = resolve_parameters(profile, {"duration": 1, "fps": 16}, {}, {}, False)
    assert values["timeline_duration"] == 1 and values["duration"] == 3
    assert patch(profile, values, raw)["frames"]["inputs"]["length"] == 49
    assert profile == original


def test_fixed_clock_frame_bounds_preserve_native_alignment(system):
    profile = configure(system, ["INT", {"min": 5, "max": 73, "step": 17}])
    profile["bindings"]["duration"].update(frame_multiple=17, frame_offset=5, frame_fps=24)
    maximum = render_maximum(profile, {"fps": 16})
    assert maximum == pytest.approx(73 / 24)
    values, _, raw, _ = resolve_parameters(profile, {"duration": 3, "fps": 16}, {}, {}, False)
    assert values["duration"] == 3 and values["fps"] == 24
    assert patch(profile, values, raw)["frames"]["inputs"]["length"] == 73


def test_no_legal_frames_fails_before_planning_or_assets(system):
    client, app, comfy = system
    configure(system, ["INT", {"min": 2, "max": 80, "step": 4}])
    id = client.post(
        "/api/v1/episodes", json={"idea": "no frame intersection", "target_duration": 5}
    ).json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    episode = wait_episode(client, id)
    assert episode["error"]["code"] == "WORKFLOW_INVALID"
    assert episode["plan"] is None and not comfy.prompts
    assert not app.state.store.list("job", id)


def test_legacy_oversized_story_fails_without_spending_or_rewriting(system):
    client, app, comfy = system
    episode = preview(system, target_duration=10, max_shot_duration=5)
    configure(system)
    # Existing approved story predates metadata changes; resume must preserve it.
    app.state.store.update(
        "episode", episode["id"], {"preview_approved_at": "approved", "status": "FAILED"}
    )
    with pytest.raises(AppError):
        profile = configure(system)
        resolve_parameters(profile, {"duration": 5, "fps": 16}, {}, {}, False)
    assert client.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 202
    failed = wait_episode(client, episode["id"])
    assert failed["status"] == "FAILED"
    assert failed["plan"] == episode["plan"] and failed["shots"] == episode["shots"]
    assert not comfy.prompts
