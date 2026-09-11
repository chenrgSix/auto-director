import pytest

from app.workflows.versions import version_matches
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview


def test_repeated_connection_checks_keep_saved_preview_valid(system):
    client, app, _ = system
    assert client.post("/api/v1/comfyui/test").status_code == 200
    prepared = preview(system)
    profile = app.state.store.get("workflow", "default_video")
    for method, path in [("GET", "status"), ("GET", "system"), ("POST", "test")]:
        assert client.request(method, f"/api/v1/comfyui/{path}").status_code == 200
    current = app.state.store.get("workflow", "default_video")
    assert current["version"] > profile["version"]
    assert current["configuration_version"] == profile["configuration_version"]
    assert current["workflow"] == profile["workflow"]
    response = client.patch(f"/api/v1/episodes/{prepared['id']}/preview", json=edits(prepared))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["plan"] == prepared["plan"]
    assert (
        client.post(
            f"/api/v1/episodes/{prepared['id']}/approve",
            json={"expected_version": saved["version"]},
        ).status_code
        == 202
    )
    assert wait_episode(client, prepared["id"])["status"] == "COMPLETED"


def test_name_and_noop_save_do_not_invalidate_preview(system):
    client, _, _ = system
    prepared = preview(system)
    for body in [{"name": "Display name only"}, {"parameter_values": {}}]:
        assert client.patch("/api/v1/workflows/default_video", json=body).status_code == 200
    response = client.post(
        f"/api/v1/episodes/{prepared['id']}/approve", json={"expected_version": prepared["version"]}
    )
    assert response.status_code == 202, response.text
    assert wait_episode(client, prepared["id"])["status"] == "COMPLETED"


def test_real_configuration_change_still_invalidates_after_checks(system):
    client, _, comfy = system
    prepared = preview(system)
    assert client.post("/api/v1/comfyui/test").status_code == 200
    assert (
        client.patch(
            "/api/v1/workflows/default_video", json={"parameter_values": {"sampler.steps": 12}}
        ).status_code
        == 200
    )
    assert client.post("/api/v1/comfyui/test").status_code == 200
    response = client.post(
        f"/api/v1/episodes/{prepared['id']}/approve", json={"expected_version": prepared["version"]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PREVIEW_STALE"
    assert not comfy.prompts


def test_legacy_workflow_version_survives_multiple_checks(system):
    client, app, _ = system
    for id in ("default_image", "default_video"):
        app.state.store.update("workflow", id, lambda p: p.pop("configuration_version"))
    prepared = preview(system)
    for _ in range(2):
        assert client.post("/api/v1/comfyui/test").status_code == 200
    response = client.patch(f"/api/v1/episodes/{prepared['id']}/preview", json=edits(prepared))
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == prepared["plan"]


def test_live_frame_changes_are_still_checked_before_rendering(system):
    client, app, comfy = system
    prepared = preview(system, target_duration=5, max_shot_duration=5)
    comfy.info["WanFirstLastFrameToVideo"]["input"]["required"]["length"] = [
        "INT",
        {"min": 1, "max": 49, "step": 4},
    ]
    assert client.post("/api/v1/comfyui/test").status_code == 200
    assert (
        client.post(
            f"/api/v1/episodes/{prepared['id']}/approve",
            json={"expected_version": prepared["version"]},
        ).status_code
        == 202
    )
    failed = wait_episode(client, prepared["id"])
    assert failed["error"]["code"] == "WORKFLOW_INVALID"
    assert failed["plan"] == prepared["plan"]
    assert not comfy.prompts and not app.state.store.list("asset", prepared["id"])


@pytest.mark.parametrize("saved,expected", [(6, False), (7, True), (8, True), (9, False)])
def test_version_interval_rejects_old_configurations_and_future_versions(saved, expected):
    assert version_matches({"version": 8, "configuration_version": 7}, saved) is expected
