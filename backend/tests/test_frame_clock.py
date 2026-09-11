from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.errors import AppError
from app.generation.parameters import duration_seconds, resolve_parameters
from app.workflows.analyzer import patch
from app.workflows.schema import Binding
from tests.test_api_pipeline import wait_episode


def configure(system):
    client, _, _ = system
    profile = client.get("/api/v1/workflows/default_video").json()
    bindings = deepcopy(profile["bindings"])
    bindings["duration"].update(
        transform="duration_to_frames", frame_multiple=17, frame_offset=5, frame_fps=24
    )
    response = client.patch("/api/v1/workflows/default_video", json={"bindings": bindings})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(("seconds", "frames"), [(1, 39), (3, 73), (5, 124)])
def test_fixed_clock_aligns_frames_and_output_fps(system, seconds, frames):
    profile = configure(system)
    original = deepcopy(profile["workflow"])
    values, _, raw, _ = resolve_parameters(profile, {"duration": seconds, "fps": 16}, {}, {}, False)
    graph = patch(profile, values, raw)
    duration, fps = profile["bindings"]["duration"], profile["bindings"]["fps"]
    assert graph[duration["node_id"]]["inputs"][duration["input"]] == frames
    assert graph[fps["node_id"]]["inputs"][fps["input"]] == 24
    assert values["timeline_duration"] == seconds
    assert profile["workflow"] == original


def test_fixed_clock_rejects_conflicting_user_fps(system):
    profile = configure(system)
    binding = profile["bindings"]["fps"]
    key = f"{binding['node_id']}.{binding['input']}"
    with pytest.raises(AppError, match="固定为 24 FPS"):
        resolve_parameters(profile, {"duration": 5}, {}, {key: 16}, True)
    with pytest.raises(AppError, match="固定为 24 FPS"):
        patch(profile, {"duration": 5}, {key: 16})
    values, _, _, _ = resolve_parameters(profile, {"duration": 5}, {}, {key: 24}, True)
    assert values["fps"] == 24


def test_binding_clock_validation_and_legacy_inverse():
    for values in [
        {"frame_fps": 0},
        {"frame_offset": 32},
        {"frame_fps": 24, "transform": "identity"},
    ]:
        with pytest.raises(ValidationError):
            Binding(node_id="clip", input="length", **values)
    legacy = {"bindings": {"duration": {"transform": "duration_to_frames", "frame_offset": 1}}}
    assert duration_seconds(legacy, 49, 16) == 3
    fixed = {
        "bindings": {
            "duration": {"transform": "duration_to_frames", "frame_offset": 5, "frame_fps": 24}
        }
    }
    assert duration_seconds(fixed, 124, 16) == pytest.approx(124 / 24)


def test_preview_and_generation_use_same_fixed_clock(system):
    client, app, _ = system
    profile = configure(system)
    from tests.test_episode_preview import preview

    prepared = preview(system, target_duration=5, max_shot_duration=5)
    assert prepared["budget"]["fps"] == 24
    assert not app.state.store.list("job", prepared["id"])
    id = prepared["id"]
    response = client.post(
        f"/api/v1/episodes/{id}/approve", json={"expected_version": prepared["version"]}
    )
    assert response.status_code == 202, response.text
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode.get("error")
    assert episode["budget"]["fps"] == 24
    assert episode["shots"][0]["duration"] == 5
    assert episode["final_duration"] == pytest.approx(5, abs=0.1)
    job = next(j for j in app.state.store.list("job", id) if j["type"] == "SHOT_VIDEO")
    binding = profile["bindings"]["duration"]
    assert job["patched_workflow"][binding["node_id"]]["inputs"][binding["input"]] == 124
    assert job["input_values"]["fps"] == 24
