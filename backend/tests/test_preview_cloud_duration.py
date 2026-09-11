from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.workflows.duration import render_maximum, uses_remote_video
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_workflow_duration import cloud_profile, install_cloud


@pytest.mark.parametrize("validate_first", [False, True])
@pytest.mark.parametrize("duration", [5, 90])
def test_cloud_preview_save_and_approve_keep_planned_render_limit(system, validate_first, duration):
    client, app, comfy = system
    wid = install_cloud(client, comfy)
    if validate_first:
        assert client.post(f"/api/v1/workflows/{wid}/validate").status_code == 200
    episode = preview(
        system,
        target_duration=duration,
        max_shot_duration=5,
        video_workflow_id=wid,
        memory_mode="low",
    )
    assert episode["budget"]["low_memory"] and episode["preview"]["remote_video"] is True
    assert episode["preview"]["render_max_duration"] == 5
    before = app.state.store.get("workflow", wid)
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=edits(episode))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["plan"] == episode["plan"]
    assert sum(s["duration"] for s in saved["shots"]) == duration
    assert not comfy.prompts
    if duration == 5:
        assert (
            client.post(
                f"/api/v1/episodes/{episode['id']}/approve",
                json={"expected_version": saved["version"]},
            ).status_code
            == 202
        )
        completed = wait_episode(client, episode["id"])
        assert completed["status"] == "COMPLETED", completed["error"]
        video = next(
            j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SHOT_VIDEO"
        )
        assert video["patched_workflow"]["clip"]["inputs"]["model.duration"] == 5
    assert app.state.store.get("workflow", wid) == before


def test_old_cloud_preview_uses_verified_duration_node_and_rejects_workflow_drift(system):
    client, app, comfy = system
    wid = install_cloud(client, comfy)
    client.post(f"/api/v1/workflows/{wid}/validate")
    episode = preview(
        system, target_duration=90, max_shot_duration=5, video_workflow_id=wid, memory_mode="low"
    )
    id = episode["id"]

    def legacy(record):
        record["preview"].pop("remote_video")

    old = app.state.store.update("episode", id, legacy)
    assert "remote_video" not in app.state.store.get("workflow", wid)
    result = client.patch(f"/api/v1/episodes/{id}/preview", json=edits(old))
    assert result.status_code == 200, result.text
    assert result.json()["plan"] == old["plan"]
    client.patch(f"/api/v1/workflows/{wid}", json={"parameter_values": {"clip.model": "max"}})
    assert (
        client.patch(f"/api/v1/episodes/{id}/preview", json=edits(result.json())).status_code == 409
    )
    assert not comfy.prompts


@pytest.mark.parametrize(
    "case", ["fresh_local", "image", "other_node", "changed_class", "label_only", "no_metadata"]
)
def test_cloud_cache_cannot_exempt_local_or_unknown_video_from_vram_limit(case):
    profile, _ = cloud_profile()
    profile.pop("remote_video")
    profile["execution_info"] = {
        "mode": "cloud",
        "api_nodes": [{"id": "clip", "class_type": "CloudVideo"}],
    }
    assert uses_remote_video(profile)
    assert render_maximum(profile, {"low_memory": True}) == 5
    invalid = deepcopy(profile)
    if case == "fresh_local":
        invalid["remote_video"] = False
    elif case == "image":
        invalid["media_type"] = "image"
    elif case == "other_node":
        invalid["execution_info"]["api_nodes"][0]["id"] = "start"
    elif case == "changed_class":
        invalid["workflow"]["clip"]["class_type"] = "AnotherNode"
    elif case == "label_only":
        invalid["execution_info"]["api_nodes"] = []
    else:
        invalid.pop("execution_info")
    assert not uses_remote_video(invalid)
    with pytest.raises(AppError, match="无可用值"):
        render_maximum(invalid, {"low_memory": True})
