from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.generation.workflow_state import story_snapshot
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import preview
from tests.test_episode_workflows import imported, rebind


def fail_after_frames(system, monkeypatch, *, target_duration=4, **preview_options):
    client, app, _ = system
    episode = preview(system, target_duration=target_duration, **preview_options)
    run = app.state.engine.run

    async def fail_video(job_id, *args, **kwargs):
        job = app.state.store.get("job", job_id)
        if job["type"] == "SHOT_VIDEO":
            error = AppError("EXECUTION_ERROR", "Fixture video failure after successful images")
            app.state.store.update("job", job_id, {"status": "FAILED", "error": error.as_dict()})
            raise error
        return await run(job_id, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(app.state.engine, "run", fail_video)
        assert (
            client.post(
                f"/api/v1/episodes/{episode['id']}/approve",
                json={"expected_version": episode["version"]},
            ).status_code
            == 202
        )
        failed = wait_episode(client, episode["id"])
    assert failed["status"] == "FAILED", failed
    assert len(failed["references"]) == 3
    assert failed["shots"][0]["start_frame_asset_id"] and failed["shots"][0]["end_frame_asset_id"]
    assert not failed["shots"][0]["video_asset_id"]
    return failed


@pytest.mark.parametrize("capability", ["FIRST_LAST_TO_VIDEO", "IMAGE_TO_VIDEO"])
def test_failed_video_rebind_reuses_images_story_and_submits_only_missing_outputs(
    system, monkeypatch, capability
):
    client, app, comfy = system
    episode = fail_after_frames(system, monkeypatch)
    id = episode["id"]
    old_jobs = app.state.store.list("job", id)
    old_assets = app.state.store.list("asset", id)
    replacement = imported(app, capability)
    before_calls = list(comfy.calls)
    saved = rebind(client, episode, video_workflow_id=replacement["id"])
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    for key in (
        "plan",
        "bible",
        "shots",
        "references",
        "continuity",
        "metrics",
        "preview_approved_at",
    ):
        assert saved[key] == episode[key]
    assert saved["status"] == "DRAFT" and saved["refresh_workflow_budget"]
    assert comfy.calls == before_calls and id not in app.state.generation.busy
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 409
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["plan"] == episode["plan"] and final["references"] == episode["references"]
    assert final["metrics"]["llm_calls"] == episode["metrics"]["llm_calls"]
    assert not final["refresh_workflow_budget"]
    old_ids = {job["id"] for job in old_jobs}
    new_jobs = [job for job in app.state.store.list("job", id) if job["id"] not in old_ids]
    assert all(job["step_key"].startswith("binding:1:") for job in new_jobs)
    assert not any(job["type"].endswith("REFERENCE") for job in new_jobs)
    first_jobs = [job for job in new_jobs if job["shot_id"] == episode["shots"][0]["id"]]
    assert len(first_jobs) == 1 and first_jobs[0]["type"] == "SHOT_VIDEO"
    assert first_jobs[0]["workflow_id"] == replacement["id"]
    assert (
        first_jobs[0]["asset_bindings"]["start_frame"]
        == episode["shots"][0]["start_frame_asset_id"]
    )
    if capability == "IMAGE_TO_VIDEO":
        assert "end_frame" not in first_jobs[0]["asset_bindings"]
        assert not any(job["type"] == "SHOT_END_FRAME" for job in new_jobs)
    else:
        assert (
            first_jobs[0]["asset_bindings"]["end_frame"]
            == episode["shots"][0]["end_frame_asset_id"]
        )
    for job in old_jobs:
        assert app.state.store.get("job", job["id"]) == job
    for asset in old_assets:
        assert app.state.store.get("asset", asset["id"]) == asset
        assert app.state.assets.path(asset["id"]).is_file()


def legacy_reset(system, episode, replacement):
    _, app, _ = system
    return app.state.store.update(
        "episode",
        episode["id"],
        {
            "status": "DRAFT",
            "plan": None,
            "bible": None,
            "shots": [],
            "references": {},
            "continuity": {},
            "budget": None,
            "preview": None,
            "preview_approved_at": None,
            "error": None,
            "video_workflow_id": replacement["id"],
            "workflow_binding_revision": 1,
            "workflow_binding_history": [
                {
                    "revision": 0,
                    "previous_state": {
                        **story_snapshot(episode),
                        "video_workflow_id": episode["video_workflow_id"],
                    },
                }
            ],
        },
    )


def restore(client, episode, revision=0):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/workflows/restore",
        json={"expected_version": episode["version"], "history_revision": revision},
    )


def test_legacy_reset_restores_story_and_media_without_reverting_selected_workflow(
    system, monkeypatch
):
    client, app, comfy = system
    original = fail_after_frames(system, monkeypatch, target_duration=1)
    replacement = imported(app, "IMAGE_TO_VIDEO")
    reset = legacy_reset(system, original, replacement)
    before_calls = list(comfy.calls)
    assert (
        client.get(f"/api/v1/episodes/{reset['id']}").json()["recoverable_workflow_revision"] == 0
    )
    response = restore(client, reset)
    assert response.status_code == 200, response.text
    saved = response.json()
    for key in (
        "plan",
        "bible",
        "shots",
        "references",
        "continuity",
        "metrics",
        "preview_approved_at",
    ):
        assert saved[key] == original[key]
    assert saved["video_workflow_id"] == replacement["id"]
    assert saved["workflow_binding_history"] == reset["workflow_binding_history"]
    assert saved["workflow_recovery"]["source_revision"] == 0
    assert saved["workflow_binding_revision"] == 1
    assert comfy.calls == before_calls and saved["id"] not in app.state.generation.busy
    assert (
        "recoverable_workflow_revision" not in client.get(f"/api/v1/episodes/{saved['id']}").json()
    )
    assert restore(client, saved).status_code == 409
    assert app.state.store.get("episode", saved["id"]) == saved
    assert client.post(f"/api/v1/episodes/{saved['id']}/generate").status_code == 202
    final = wait_episode(client, saved["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["references"] == original["references"]
    assert final["metrics"]["llm_calls"] == original["metrics"]["llm_calls"]


@pytest.mark.parametrize(
    "guard",
    [
        "story",
        "stale",
        "revision",
        "active",
        "busy",
        "UNKNOWN",
        "current_job",
        "missing_asset",
        "foreign_asset",
    ],
)
def test_legacy_restore_rejects_conflicts_and_missing_or_foreign_files_atomically(
    system, monkeypatch, guard
):
    client, app, _ = system
    original = fail_after_frames(system, monkeypatch, target_duration=1)
    episode = legacy_reset(system, original, imported(app, "IMAGE_TO_VIDEO"))
    id = episode["id"]
    if guard == "story":
        episode = app.state.store.update("episode", id, {"plan": {"title": "Keep current story"}})
    elif guard == "stale":
        app.state.store.update("episode", id, {"title": "Concurrent edit"})
    elif guard == "active":
        episode = app.state.store.update("episode", id, {"status": "RENDERING_VIDEO"})
    elif guard == "busy":
        app.state.generation.busy.add(id)
    elif guard in {"UNKNOWN", "current_job"}:
        app.state.store.create(
            "job",
            {
                "status": "UNKNOWN" if guard == "UNKNOWN" else "FAILED",
                "step_key": "binding:1:fixture",
            },
            parent=id,
        )
    elif guard == "missing_asset":
        app.state.assets.path(original["shots"][0]["start_frame_asset_id"]).unlink()
    elif guard == "foreign_asset":
        app.state.store.update(
            "asset", original["shots"][0]["start_frame_asset_id"], {"episode_id": "another-episode"}
        )
    before = deepcopy(app.state.store.get("episode", id))
    try:
        response = restore(client, episode, 9 if guard == "revision" else 0)
        assert response.status_code == (
            404 if guard == "missing_asset" else 400 if guard == "foreign_asset" else 409
        ), response.text
        assert app.state.store.get("episode", id) == before
    finally:
        app.state.generation.busy.discard(id)


def test_incompatible_render_workflow_fails_before_new_media_and_keeps_progress(
    system, monkeypatch
):
    client, app, comfy = system
    original = fail_after_frames(system, monkeypatch)
    replacement = imported(app, "IMAGE_TO_VIDEO")
    app.state.store.update(
        "workflow",
        replacement["id"],
        {"capabilities": {**replacement["capabilities"], "max_duration": 1}},
    )
    saved = rebind(client, original, video_workflow_id=replacement["id"]).json()
    before_prompts = deepcopy(comfy.prompts)
    before_jobs = app.state.store.list("job", original["id"])
    assert client.post(f"/api/v1/episodes/{saved['id']}/generate").status_code == 202
    failed = wait_episode(client, saved["id"])
    assert failed["status"] == "FAILED" and failed["error"]
    for key in ("plan", "bible", "shots", "references", "continuity"):
        assert failed[key] == original[key]
    assert comfy.prompts == before_prompts
    assert app.state.store.list("job", original["id"]) == before_jobs


@pytest.mark.parametrize("case", ["stale_budget", "edited_workflow"])
def test_rebound_progress_uses_live_limits_and_never_loses_approval(system, monkeypatch, case):
    client, app, _ = system
    original = fail_after_frames(system, monkeypatch)
    if case == "stale_budget":
        original = app.state.store.update(
            "episode",
            original["id"],
            {
                "budget": {
                    **original["budget"],
                    "max_duration": 1,
                    "render_max_duration": 1,
                    "low_memory": True,
                    "vram_free": 1024**3,
                },
            },
        )
    replacement = imported(app, "IMAGE_TO_VIDEO")
    saved = rebind(client, original, video_workflow_id=replacement["id"]).json()
    if case == "edited_workflow":
        app.state.store.update("workflow", replacement["id"], {"name": "Updated before resume"})
    assert client.post(f"/api/v1/episodes/{saved['id']}/generate").status_code == 202
    final = wait_episode(client, saved["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["preview_approved_at"] == original["preview_approved_at"]
    assert final["plan"] == original["plan"] and final["references"] == original["references"]
    assert final["metrics"]["llm_calls"] == original["metrics"]["llm_calls"]
    assert not final["budget"]["low_memory"]


def test_rebinding_retains_authorization_for_uploaded_frames_already_in_use(system, monkeypatch):
    client, app, comfy = system
    original = fail_after_frames(system, monkeypatch, target_duration=1)
    upload = client.post(
        "/api/v1/assets", files={"file": ("frame.png", comfy.image_bytes, "image/png")}
    ).json()

    def replace_frame(episode):
        episode["shots"][0]["start_frame_asset_id"] = upload["id"]
        episode["allowed_asset_ids"] = [upload["id"]]

    original = app.state.store.update("episode", original["id"], replace_frame)
    replacement = imported(app, "IMAGE_TO_VIDEO")
    saved = rebind(client, original, video_workflow_id=replacement["id"]).json()
    assert saved["allowed_asset_ids"] == [upload["id"]]
    assert client.post(f"/api/v1/episodes/{saved['id']}/generate").status_code == 202
    final = wait_episode(client, saved["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["shots"][0]["start_frame_asset_id"] == upload["id"]
