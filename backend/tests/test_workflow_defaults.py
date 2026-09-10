import pytest


@pytest.mark.parametrize("media", ["image", "video"])
@pytest.mark.parametrize("state", ["unchecked", "failed", "saved_again"])
def test_default_selection_needs_bindings_but_not_dependency_check(
    system, monkeypatch, media, state
):
    client, app, comfy = system
    original = app.state.store.get("workflow", f"default_{media}")
    before = list(comfy.calls)

    def forbidden(*args, **kwargs):
        raise AssertionError("Selecting a default must not contact ComfyUI or AI")

    monkeypatch.setattr(app.state.engine, "client", forbidden)
    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    response = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "New default",
            "capability": original["capability"],
            "workflow": original["workflow"],
        },
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["validation"] is None and profile["binding_issues"] == []
    path = f"/api/v1/workflows/{profile['id']}"
    if state != "unchecked":
        app.state.store.update(
            "workflow",
            profile["id"],
            {"validation": {"valid": state == "saved_again", "issues": []}},
        )
    if state == "saved_again":
        saved = client.patch(path, json={"name": "Saved before selecting default"})
        assert saved.status_code == 200 and saved.json()["validation"] is None

    selected = client.post(f"{path}/default")
    assert selected.status_code == 200, selected.text
    settings = client.get("/api/v1/settings").json()
    assert settings[f"default_{media}"] == profile["id"]
    assert settings["default_capabilities"][profile["capability"]] == profile["id"]
    assert settings[f"default_{'video' if media == 'image' else 'image'}"] == (
        "default_video" if media == "image" else "default_image"
    )
    current = client.get(path).json()
    assert current["validation"] == ({"valid": False, "issues": []} if state == "failed" else None)
    assert current["last_test_job_id"] is None
    assert comfy.calls == before


def test_incomplete_default_is_rejected_without_changing_settings(system):
    client, app, _ = system
    original = app.state.store.get("workflow", "default_image")
    before = client.get("/api/v1/settings").json()
    response = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "Incomplete",
            "capability": "TEXT_TO_IMAGE",
            "workflow": original["workflow"],
            "bindings": {},
        },
    )
    assert response.status_code == 201
    profile = response.json()
    assert profile["binding_issues"]
    selected = client.post(f"/api/v1/workflows/{profile['id']}/default")
    assert selected.status_code == 400
    assert selected.json()["error"]["code"] == "WORKFLOW_INVALID"
    assert client.get("/api/v1/settings").json() == before
