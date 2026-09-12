"""Semantic QA policy changes preserve media and cannot bypass technical recovery."""

from copy import deepcopy

import pytest

from app.generation.schemas import EpisodeCreate
from tests.test_episode_workflows import create


def change(client, episode, policy="advisory", **extra):
    return client.patch(
        f"/api/v1/episodes/{episode['id']}/qa-policy",
        json={"expected_version": episode["version"], "qa_policy": policy, **extra},
    )


def stored_shot(id, *, code="QA_FAILED", status="FAILED", video=True):
    return {
        "id": id,
        "status": status,
        "error": {"code": code, "message": "Saved failure"} if code else None,
        "retry_version": 2,
        "seed_offset": 20000,
        "render_cursor": {"attempt": 2, "remaining": 0, "retry_version": 2},
        "qa_retry": {
            "pending": True,
            "scope": "keyframes",
            "failed_frames": ["end_frame"],
            "rejected_assets": {"end_frame_asset_id": f"{id}:end"},
        },
        "qa_frame_corrections": {"end_frame": "Fix background."},
        "start_frame_asset_id": f"{id}:start",
        "end_frame_asset_id": f"{id}:end",
        "video_asset_id": f"{id}:video" if video else None,
        "actual_end_frame_asset_id": f"{id}:actual" if video else None,
        "prompts": {"start_frame_prompt": "Reviewed start", "video_prompt": "Reviewed motion"},
        "qa": [{"stage": "keyframes", "explanation": "Historical QA", "retry_scope": "keyframes"}],
        "needs_review": True,
        "review_notes": [{"stage": "video", "message": "Existing review note"}],
    }


def stored_episode(system, **updates):
    client, app, _ = system
    episode = create(client)
    return app.state.store.update(
        "episode",
        episode["id"],
        {
            "status": "FAILED",
            "error": {"code": "QA_FAILED", "message": "Semantic QA stopped this episode"},
            "title": "Reviewed story",
            "plan": {"title": "Reviewed story", "shots": ["Saved plan"]},
            "bible": {"characters": [{"id": "chicken", "description": "Saved identity"}]},
            "references": {"character:chicken": "reference:chicken"},
            "shots": [stored_shot("shot")],
            "continuity": {"saved": "continuity"},
            "preview_approved_at": "2026-09-12T01:00:00Z",
            "budget": {"width": 384, "height": 672, "fps": 16, "max_retries": 2},
            "warnings": ["Existing warning"],
            **updates,
        },
    )


def test_creation_and_missing_legacy_policy_preserve_strict_defaults(system):
    client, app, _ = system
    assert EpisodeCreate(idea="Story", target_duration=4).qa_policy == "strict"
    strict = create(client)
    assert strict["qa_policy"] == "strict"
    advisory = create(client, qa_policy="advisory")
    assert advisory["qa_policy"] == "advisory"
    legacy = app.state.store.update("episode", strict["id"], lambda item: item.pop("qa_policy"))
    assert change(client, legacy, "strict").json() == legacy
    saved = change(client, legacy).json()
    assert saved["qa_policy"] == "advisory"
    assert saved["qa_policy_history"][-1]["qa_policy"] == "strict"


@pytest.mark.parametrize("code", ["QA_FAILED", "KEYFRAMES_TOO_SIMILAR"])
@pytest.mark.parametrize("video", [True, False])
def test_semantic_policy_change_preserves_story_media_and_history_without_generating(
    system, monkeypatch, code, video
):
    client, app, comfy = system
    before = stored_episode(
        system,
        error={"code": code, "message": "Saved episode error"},
        shots=[stored_shot("shot", code=code, video=video)],
    )
    old_job = app.state.store.create("job", {"status": "COMPLETED"}, parent=before["id"])
    media = app.state.assets.root / "preserved.bin"
    media.write_bytes(b"Existing rendered media must not change")
    old_asset = app.state.store.create(
        "asset", {"path": "preserved.bin", "type": "SHOT_VIDEO"}, parent=before["id"]
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Policy settings cannot call external services or enqueue generation")

    monkeypatch.setattr(app.state.engine, "client", forbidden)
    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    monkeypatch.setattr(app.state.generation, "enqueue", forbidden)
    response = change(client, before)
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["qa_policy"] == "advisory"
    assert after["status"] == "DRAFT" and after["error"] is None
    assert after["version"] == before["version"] + 1
    original = before["shots"][0]
    saved = after["shots"][0]
    assert saved["status"] == ("VIDEO_READY" if video else "PENDING")
    assert saved["retry_version"] == original["retry_version"] + 1
    assert saved["error"] is None
    for field in ("render_cursor", "qa_retry", "qa_frame_corrections"):
        assert field not in saved
    mutable_shot_fields = {
        "status",
        "error",
        "retry_version",
        "render_cursor",
        "qa_retry",
        "qa_frame_corrections",
    }
    for field, value in original.items():
        if field not in mutable_shot_fields:
            assert saved[field] == value, field
    mutable_fields = {
        "qa_policy",
        "qa_policy_history",
        "status",
        "error",
        "version",
        "updated_at",
        "shots",
    }
    for field, value in before.items():
        if field not in mutable_fields:
            assert after[field] == value, field
    history = after["qa_policy_history"][-1]
    assert history["qa_policy"] == "strict" and history["error"] == before["error"]
    assert history["status"] == "FAILED"
    for field, value in history["shot_execution"]["shot"].items():
        assert value == original.get(field), field
    assert app.state.store.get("job", old_job["id"]) == old_job
    assert app.state.store.get("asset", old_asset["id"]) == old_asset
    assert media.read_bytes() == b"Existing rendered media must not change"
    assert not comfy.calls and not app.state.generation.busy


@pytest.mark.parametrize("code", ["VIDEO_TOO_SHORT", "OUT_OF_MEMORY", "SUBMISSION_UNKNOWN"])
def test_policy_change_keeps_technical_failure_and_retry_identity(system, code):
    client, app, _ = system
    before = stored_episode(
        system,
        error={"code": code, "message": "Technical failure"},
        shots=[stored_shot("shot", code=code)],
        render_recovery={"shot_id": "shot", "cursor": {"attempt": 2}, "jobs": {}},
    )
    after = change(client, before).json()
    for field in ("status", "error", "shots", "render_recovery", "budget"):
        assert after[field] == before[field], field
    assert after["qa_policy_history"][-1]["render_recovery"] == before["render_recovery"]
    assert app.state.store.list("job", before["id"]) == []


def test_policy_change_keeps_submitted_recovery_cursor_even_with_old_semantic_error(system):
    client, _, _ = system
    before = stored_episode(
        system, render_recovery={"shot_id": "shot", "cursor": {"attempt": 2}, "jobs": {}}
    )
    after = change(client, before).json()
    assert after["render_recovery"] == before["render_recovery"]
    assert after["shots"] == before["shots"]


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
def test_completed_media_passed_shots_and_unrelated_states_remain_available(system, status):
    client, _, _ = system
    before = stored_episode(
        system,
        qa_policy="advisory",
        status=status,
        error=None,
        shots=[stored_shot("shot", status="PASSED", code=None)],
        final_video_asset_id="completed:film",
        final_duration=4,
    )
    after = change(client, before, "strict").json()
    for field in (
        "status",
        "error",
        "shots",
        "final_video_asset_id",
        "final_duration",
        "plan",
        "bible",
    ):
        assert after[field] == before[field], field


@pytest.mark.parametrize(
    "state", ["PLANNING", "RENDERING_VIDEO", "busy", "QUEUED_JOB", "RUNNING_JOB", "UNKNOWN_JOB"]
)
def test_policy_change_blocks_active_and_unsettled_work(system, state):
    client, app, comfy = system
    before = create(client)
    id = before["id"]
    if state == "busy":
        app.state.generation.busy.add(id)
    elif state.endswith("_JOB"):
        app.state.store.create("job", {"status": state.removesuffix("_JOB")}, parent=id)
    else:
        before = app.state.store.update("episode", id, {"status": state})
    try:
        response = change(client, before)
        assert response.status_code == 409, response.text
        assert app.state.store.get("episode", id) == before and not comfy.calls
    finally:
        app.state.generation.busy.discard(id)


@pytest.mark.parametrize(
    "extra",
    [
        {"qa_policy": "off"},
        {"qa_policy": None},
        {"quality": "fast"},
        {"shots": []},
        {"qa_enabled": False},
    ],
)
def test_policy_request_cannot_edit_unrelated_fields_or_accept_invalid_values(system, extra):
    client, app, _ = system
    before = create(client)
    response = (
        change(client, before, **extra)
        if "qa_policy" not in extra
        else change(client, before, extra["qa_policy"])
    )
    assert response.status_code == 422, response.text
    assert app.state.store.get("episode", before["id"]) == before


def test_policy_stale_versions_are_rejected_and_same_policy_is_noop(system):
    client, app, _ = system
    before = stored_episode(system)
    assert change(client, before, "strict").json() == before
    current = app.state.store.update("episode", before["id"], {"title": "User changed the title"})
    assert change(client, before).status_code == 409
    assert app.state.store.get("episode", before["id"]) == current


def test_policy_archives_each_change_without_rewriting_earlier_history(system):
    client, _, _ = system
    first = change(client, create(client)).json()
    original_history = deepcopy(first["qa_policy_history"])
    second = change(client, first, "strict").json()
    assert len(second["qa_policy_history"]) == 2
    assert second["qa_policy_history"][:1] == original_history
    assert second["qa_policy_history"][1]["qa_policy"] == "advisory"
