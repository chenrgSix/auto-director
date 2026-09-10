from copy import deepcopy

import pytest

from app.workflows.schema import WorkflowImport
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_image_graph, image_to_video_graph


def imported(app, capability="TEXT_TO_IMAGE", *, incomplete=False):
    media = "image" if capability.endswith("TO_IMAGE") else "video"
    base = app.state.store.get("workflow", f"default_{media}")["workflow"]
    graph = (
        image_to_video_graph(base)
        if capability == "IMAGE_TO_VIDEO"
        else image_to_image_graph(base)
        if capability == "IMAGE_TO_IMAGE"
        else base
    )
    return app.state.workflows.import_workflow(
        WorkflowImport(
            name="Replacement",
            capability=capability,
            workflow=graph,
            **({"bindings": {}} if incomplete else {}),
        )
    )


def create(client, **extra):
    response = client.post(
        "/api/v1/episodes", json={"idea": "Replacement fixture", "target_duration": 4, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


def rebind(client, episode, **selection):
    return client.patch(
        f"/api/v1/episodes/{episode['id']}/workflows",
        json={
            "expected_version": episode["version"],
            **{
                key: episode[key]
                for key in ("image_workflow_id", "reference_workflow_id", "video_workflow_id")
            },
            **selection,
        },
    )


def test_rebind_archives_state_preserves_history_and_only_retains_relevant_overrides(
    system, monkeypatch
):
    client, app, comfy = system
    episode = create(
        client,
        advanced_mode=True,
        image_parameters={"sampler.steps": 9},
        video_parameters={"sampler.steps": 7},
        workflow_overrides={
            "default_image": {"sampler.cfg": 3},
            "default_video": {"sampler.cfg": 2},
        },
    )
    id = episode["id"]
    episode = app.state.store.update(
        "episode",
        id,
        {
            "status": "FAILED",
            "plan": {"title": "Existing story"},
            "bible": {"ai_parameters": {"default_image": {"positive.text": "old"}}},
            "shots": [{"id": "old-shot", "video_asset_id": "old-video"}],
            "references": {"style": "old-image"},
            "budget": {"max_duration": 5},
            "final_video_asset_id": "old-final",
            "final_duration": 4,
            "metrics": {"llm_calls": 2},
            "continuity": {"state": "old"},
        },
    )
    job = app.state.store.create(
        "job", {"status": "FAILED", "patched_workflow": {"old": "graph"}}, parent=id
    )
    asset = app.state.store.create("asset", {"episode_id": id, "path": "historical.mp4"}, parent=id)
    replacement = imported(app)
    settings = client.get("/api/v1/settings").json()
    before_calls = list(comfy.calls)

    def forbidden(*args, **kwargs):
        raise AssertionError("Rebinding must not call external services or start generation")

    monkeypatch.setattr(app.state.engine, "client", forbidden)
    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    result = rebind(
        client,
        episode,
        image_workflow_id=replacement["id"],
        reference_workflow_id=replacement["id"],
    )
    assert result.status_code == 200, result.text
    changed = result.json()
    assert changed["status"] == "DRAFT" and changed["workflow_binding_revision"] == 1
    assert changed["idea"] == episode["idea"] and changed["target_duration"] == 4
    assert changed["plan"] is None and changed["bible"] is None and changed["budget"] is None
    assert changed["shots"] == [] and changed["references"] == {} and changed["continuity"] == {}
    assert changed["final_video_asset_id"] is None and changed["final_duration"] is None
    assert changed["metrics"] == episode["metrics"]
    assert changed["image_parameters"] == {}
    assert changed["video_parameters"] == {"sampler.steps": 7}
    assert changed["workflow_overrides"] == {"default_video": {"sampler.cfg": 2}}
    archived = changed["workflow_binding_history"][0]["previous_state"]
    for key in (
        "plan",
        "bible",
        "shots",
        "references",
        "budget",
        "final_video_asset_id",
        "image_parameters",
        "workflow_overrides",
    ):
        assert archived[key] == episode[key]
    assert app.state.store.get("job", job["id"]) == job
    assert app.state.store.get("asset", asset["id"]) == asset
    assert client.get("/api/v1/settings").json() == settings
    assert comfy.calls == before_calls and id not in app.state.generation.busy


@pytest.mark.parametrize("guard", ["active", "busy", "QUEUED", "RUNNING", "UNKNOWN", "stale"])
def test_rebind_rejects_unsettled_or_stale_updates_atomically(system, guard):
    client, app, _ = system
    episode = create(client)
    id = episode["id"]
    replacement = imported(app)
    if guard == "active":
        episode = app.state.store.update("episode", id, {"status": "RENDERING_VIDEO"})
    elif guard == "busy":
        app.state.generation.busy.add(id)
    elif guard == "stale":
        app.state.store.update("episode", id, {"title": "Changed elsewhere"})
    else:
        app.state.store.create("job", {"status": guard}, parent=id)
    before = app.state.store.get("episode", id)
    try:
        response = rebind(client, episode, image_workflow_id=replacement["id"])
        assert response.status_code == 409, response.text
        assert app.state.store.get("episode", id) == before
    finally:
        app.state.generation.busy.discard(id)


@pytest.mark.parametrize("invalid", ["missing", "media", "reference_capability", "bindings"])
def test_rebind_checks_workflow_identity_and_bindings_atomically(system, invalid):
    client, app, _ = system
    episode = create(client)
    selection = {"image_workflow_id": "not-found"}
    if invalid == "media":
        selection = {"video_workflow_id": "default_image"}
    elif invalid == "reference_capability":
        selection = {"reference_workflow_id": imported(app, "IMAGE_TO_IMAGE")["id"]}
    elif invalid == "bindings":
        selection = {"image_workflow_id": imported(app, incomplete=True)["id"]}
    result = rebind(client, episode, **selection)
    assert result.status_code == (404 if invalid == "missing" else 400), result.text
    assert app.state.store.get("episode", episode["id"]) == episode


def test_unchanged_selection_keeps_progress_and_does_not_add_history(system):
    client, app, _ = system
    episode = create(client)
    episode = app.state.store.update(
        "episode",
        episode["id"],
        {"status": "COMPLETED", "final_video_asset_id": "keep", "plan": {"title": "keep"}},
    )
    changed = rebind(client, episode).json()
    assert changed["status"] == "COMPLETED" and changed["final_video_asset_id"] == "keep"
    assert changed["plan"] == episode["plan"] and not changed.get("workflow_binding_history")


def test_rebind_preserves_retained_upload_override_then_removes_it_when_video_changes(system):
    client, app, comfy = system
    upload = client.post(
        "/api/v1/assets", files={"file": ("frame.png", comfy.image_bytes, "image/png")}
    ).json()
    episode = create(
        client,
        advanced_mode=True,
        workflow_overrides={"default_video": {"start.image": upload["id"]}},
    )
    image = imported(app)
    changed = rebind(client, episode, image_workflow_id=image["id"]).json()
    assert changed["allowed_asset_ids"] == [upload["id"]]
    assert changed["workflow_overrides"] == episode["workflow_overrides"]
    video = imported(app, "IMAGE_TO_VIDEO")
    response = rebind(client, changed, video_workflow_id=video["id"])
    assert response.status_code == 200, response.text
    changed_again = response.json()
    assert changed_again["workflow_overrides"] == {} and changed_again["allowed_asset_ids"] == []
    assert changed_again["workflow_binding_revision"] == 2
    assert len(changed_again["workflow_binding_history"]) == 2
    assert changed_again["workflow_binding_history"][0] == changed["workflow_binding_history"][0]
    assert all(
        "workflow_binding_history" not in item["previous_state"]
        for item in changed_again["workflow_binding_history"]
    )
    assert app.state.assets.path(upload["id"]).exists()


def test_invalid_retained_override_rejects_rebind_without_partial_reset(system):
    client, app, _ = system
    episode = create(client, advanced_mode=True, video_parameters={"sampler.steps": 7})
    app.state.store.update(
        "workflow",
        "default_video",
        {"parameter_rules": {"sampler.steps": {"editable": False, "override_policy": "never"}}},
    )
    response = rebind(client, episode, image_workflow_id=imported(app)["id"])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OVERRIDE_NOT_ALLOWED"
    assert app.state.store.get("episode", episode["id"]) == episode


@pytest.mark.parametrize("replacement_type", ["identical_graph", "short_i2v"])
def test_rebound_episode_regenerates_with_new_workflows_and_never_reuses_old_cache(
    system, replacement_type
):
    client, app, comfy = system
    episode = create(client, width=256, height=256)
    id = episode["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    original = wait_episode(client, id)
    assert original["status"] == "COMPLETED", original.get("error")
    old_jobs = deepcopy(app.state.store.list("job", id))
    old_assets = deepcopy(app.state.store.list("asset", id))
    old_job_ids = {job["id"] for job in old_jobs}
    old_reference_ids = set(original["references"].values())
    image = imported(app)
    selection = {"image_workflow_id": image["id"], "reference_workflow_id": image["id"]}
    if replacement_type == "short_i2v":
        video = imported(app, "IMAGE_TO_VIDEO")
        app.state.store.update(
            "workflow", video["id"], {"capabilities": {**video["capabilities"], "max_duration": 2}}
        )
        selection["video_workflow_id"] = video["id"]
    response = rebind(client, original, **selection)
    assert response.status_code == 200, response.text
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    regenerated = wait_episode(client, id)
    assert regenerated["status"] == "COMPLETED", regenerated.get("error")
    assert abs(regenerated["final_duration"] - 4) < 0.15
    assert set(regenerated["references"].values()).isdisjoint(old_reference_ids)
    new_jobs = [job for job in app.state.store.list("job", id) if job["id"] not in old_job_ids]
    assert new_jobs and all(job["step_key"].startswith("binding:1:") for job in new_jobs)
    assert all(
        job["workflow_id"] == image["id"]
        for job in new_jobs
        if job["profile_snapshot"]["media_type"] == "image"
    )
    assert all(job["comfy_prompt_id"] in comfy.prompts for job in new_jobs)
    for job in old_jobs:
        assert app.state.store.get("job", job["id"]) == job
    for asset in old_assets:
        assert app.state.store.get("asset", asset["id"]) == asset
        assert app.state.assets.path(asset["id"]).exists()
    if replacement_type == "short_i2v":
        assert len(regenerated["shots"]) > len(original["shots"])
        assert all(shot["duration"] <= 2 for shot in regenerated["shots"])
        assert all(job["type"] != "SHOT_END_FRAME" for job in new_jobs)
        assert all(
            job["workflow_id"] == video["id"]
            for job in new_jobs
            if job["profile_snapshot"]["media_type"] == "video"
        )
