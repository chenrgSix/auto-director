from app.generation.workflow_state import REFERENCE_INPUT_WARNING
from tests.test_api_pipeline import wait_episode
from tests.test_episode_rerun import complete
from tests.test_episode_workflows import imported, rebind

OTHER_WARNING = "保留其他历史诊断"


def test_rebinding_to_image_to_image_clears_only_obsolete_reference_warning(system):
    client, app, _ = system
    old = complete(system)
    assert REFERENCE_INPUT_WARNING in old["warnings"]
    old = app.state.store.update(
        "episode", old["id"], {"warnings": [REFERENCE_INPUT_WARNING, OTHER_WARNING]}
    )
    replacement = imported(app, "IMAGE_TO_IMAGE")
    response = rebind(client, old, image_workflow_id=replacement["id"])
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["warnings"] == [OTHER_WARNING]
    assert current["workflow_binding_history"][-1]["previous_state"]["warnings"] == old["warnings"]
    for field in ("plan", "bible", "shots", "references", "final_video_asset_id"):
        assert current[field] == old[field]


def test_legacy_warning_is_filtered_on_read_without_writes_or_remote_calls(system):
    client, app, comfy = system
    replacement = imported(app, "IMAGE_TO_IMAGE")
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "stale warning",
            "target_duration": 1,
            "image_workflow_id": replacement["id"],
        },
    ).json()["id"]
    before = app.state.store.update(
        "episode", id, {"warnings": [REFERENCE_INPUT_WARNING, OTHER_WARNING]}
    )
    calls = list(comfy.calls)
    result = client.get(f"/api/v1/episodes/{id}").json()
    assert result == {**before, "warnings": [OTHER_WARNING]}
    assert app.state.store.get("episode", id) == before
    assert comfy.calls == calls


def test_warning_uses_actual_binding_not_the_capability_label(system):
    client, app, _ = system
    id = client.post(
        "/api/v1/episodes", json={"idea": "missing binding", "target_duration": 1}
    ).json()["id"]
    before = app.state.store.update("episode", id, {"warnings": [REFERENCE_INPUT_WARNING]})
    # Legacy metadata can claim I2I without a corresponding input binding.
    app.state.store.update(
        "workflow", before["image_workflow_id"], {"capability": "IMAGE_TO_IMAGE"}
    )
    assert client.get(f"/api/v1/episodes/{id}").json()["warnings"] == [REFERENCE_INPUT_WARNING]
    app.state.store.update("episode", id, {"image_workflow_id": "missing-workflow"})
    response = client.get(f"/api/v1/episodes/{id}")
    assert response.status_code == 200
    assert response.json()["warnings"] == [REFERENCE_INPUT_WARNING]


def test_generation_persists_warning_cleanup_and_still_binds_real_reference(system):
    client, app, comfy = system
    replacement = imported(app, "IMAGE_TO_IMAGE")
    comfy.info["VAEEncode"] = {"input": {"required": {"pixels": ["IMAGE"], "vae": ["VAE"]}}}
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "old warning",
            "image_workflow_id": replacement["id"],
            "target_duration": 1,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
        },
    ).json()["id"]
    app.state.store.update("episode", id, {"warnings": [REFERENCE_INPUT_WARNING, OTHER_WARNING]})
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    result = wait_episode(client, id)
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["warnings"] == app.state.store.get("episode", id)["warnings"] == [OTHER_WARNING]
    first = next(j for j in app.state.store.list("job", id) if j["type"] == "SHOT_START_FRAME")
    assert first["asset_bindings"]["reference_image"] in result["references"].values()
    assert first["patched_workflow"]["reference"]["inputs"]["image"].startswith("autodirector/")
