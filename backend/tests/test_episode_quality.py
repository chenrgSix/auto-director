"""Quality changes must leave a reviewed story and its existing media intact."""

from copy import deepcopy

import pytest

from app.agents.directing import Directors
from app.agents.schemas import QAResult
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_episode_workflows import create
from tests.test_rendered_story_rebind import fail_after_frames


def change(client, episode, quality, **extra):
    return client.patch(
        f"/api/v1/episodes/{episode['id']}/quality",
        json={"expected_version": episode["version"], "quality": quality, **extra},
    )


@pytest.mark.parametrize(
    ("quality", "retries", "candidates"), [("fast", 1, 1), ("standard", 2, 1), ("high", 3, 2)]
)
def test_reviewed_story_changes_policy_without_external_calls_or_replanning(
    system, monkeypatch, quality, retries, candidates
):
    client, app, comfy = system
    before = preview(system, quality="standard" if quality == "high" else "high", max_retries=5)
    calls = deepcopy(comfy.calls)

    def forbidden(*args, **kwargs):
        raise AssertionError("Saving a quality setting must not call external services")

    monkeypatch.setattr(app.state.engine, "client", forbidden)
    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    response = change(client, before, quality)
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["quality"] == quality and after["max_retries"] is None
    assert after["budget"] == {**before["budget"], "max_retries": retries, "candidates": candidates}
    for key in (
        "idea",
        "title",
        "plan",
        "bible",
        "shots",
        "references",
        "continuity",
        "status",
        "preview",
        "preview_approved_at",
        "workflow_overrides",
        "allowed_asset_ids",
        "qa_enabled",
    ):
        assert after.get(key) == before.get(key), key
    history = after["generation_settings_history"][-1]
    assert history["quality"] == before["quality"] and history["max_retries"] == 5
    assert history["budget"] == before["budget"]
    assert comfy.calls == calls and not app.state.store.list("job", before["id"])
    # The reviewed prompts remain editable and do not require generating a new story.
    saved = client.patch(f"/api/v1/episodes/{before['id']}/preview", json=edits(after))
    assert saved.status_code == 200, saved.text


class StandardQAProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        result = await super().generate_json(system, context, schema, images=images)
        if schema is QAResult:
            result.action_accuracy = 0.75  # Fails high, passes standard.
        return result


@pytest.mark.parametrize("keep_frames", [True, False])
def test_cancelled_high_quality_can_continue_with_standard_budget_and_preserved_story(
    system, monkeypatch, keep_frames
):
    client, app, comfy = system
    failed = fail_after_frames(
        system,
        monkeypatch,
        target_duration=5,
        max_shot_duration=5,
        quality="high",
        max_retries=3,
        qa_enabled=True,
    )
    id = failed["id"]
    shots = deepcopy(failed["shots"])
    cursor = {
        "attempt": 3,
        "remaining": 0,
        "retry_version": 0,
        "retry_scope": "keyframes",
        "base": deepcopy(failed["budget"]),
    }
    shots[0]["render_cursor"] = cursor
    shots[0]["status"] = "GENERATING_START_FRAME"
    if not keep_frames:
        shots[0].update(start_frame_asset_id=None, end_frame_asset_id=None)
    cancelled = app.state.store.update(
        "episode",
        id,
        {
            "status": "CANCELLED",
            "shots": shots,
            "render_recovery": {"shot_id": shots[0]["id"], "cursor": cursor, "jobs": {}},
        },
    )
    old_jobs = app.state.store.list("job", id)
    old_assets = app.state.store.list("asset", id)
    files = {a["id"]: app.state.assets.path(a["id"]).read_bytes() for a in old_assets}
    original_calls = len(comfy.prompts)
    saved = change(client, cancelled, "standard")
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved["status"] == "CANCELLED" and len(comfy.prompts) == original_calls
    assert saved["budget"]["candidates"] == 1 and saved["budget"]["max_retries"] == 2
    assert not saved.get("render_recovery") and not saved["shots"][0].get("render_cursor")
    assert saved["shots"][0]["retry_version"] == shots[0]["retry_version"] + 1
    assert (
        saved["generation_settings_history"][-1]["shot_execution"][shots[0]["id"]]["render_cursor"]
        == cursor
    )
    for role in ("start_frame_asset_id", "end_frame_asset_id", "video_asset_id"):
        assert saved["shots"][0][role] == shots[0][role]

    async def forbidden(*args, **kwargs):
        raise AssertionError("A quality change must reuse the script, Bible and prompts")

    for method in ("plan", "bible", "shot"):
        monkeypatch.setattr(Directors, method, forbidden)
    app.state.generation.provider_factory = StandardQAProvider
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["plan"] == failed["plan"] and final["bible"] == failed["bible"]
    assert final["references"] == failed["references"]
    assert final["shots"][0]["prompts"] == failed["shots"][0]["prompts"]
    assert final["final_duration"] == pytest.approx(5, abs=0.1)
    old_ids = {j["id"] for j in old_jobs}
    new = [j for j in app.state.store.list("job", id) if j["id"] not in old_ids]
    assert sorted(j["type"] for j in new) == (
        ["SHOT_VIDEO"] if keep_frames else ["SHOT_END_FRAME", "SHOT_START_FRAME", "SHOT_VIDEO"]
    )
    assert all(":r1:" in j["step_key"] for j in new)
    assert all(
        j["budget_snapshot"]["candidates"] == 1 and j["budget_snapshot"]["max_retries"] == 2
        for j in new
    )
    for job in old_jobs:
        assert app.state.store.get("job", job["id"]) == job
    for asset in old_assets:
        assert app.state.store.get("asset", asset["id"]) == asset
        assert app.state.assets.path(asset["id"]).read_bytes() == files[asset["id"]]


def test_completed_film_and_passed_shots_stay_available_after_quality_change(system):
    client, app, _ = system
    created = create(client, target_duration=1, quality="high", qa_enabled=False)
    assert client.post(f"/api/v1/episodes/{created['id']}/generate").status_code == 202
    before = wait_episode(client, created["id"])
    assert before["status"] == "COMPLETED", before.get("error")
    old_file = client.get(f"/api/v1/assets/{before['final_video_asset_id']}/file").content
    saved = change(client, before, "standard")
    assert saved.status_code == 200, saved.text
    after = saved.json()
    for key in (
        "status",
        "shots",
        "references",
        "final_video_asset_id",
        "final_duration",
        "plan",
        "bible",
    ):
        assert after[key] == before[key]
    assert client.get(f"/api/v1/assets/{after['final_video_asset_id']}/file").content == old_file
    assert before["id"] not in app.state.generation.busy


def test_lower_quality_rechecks_exhausted_qa_frames_before_regenerating(system):
    client, app, comfy = system

    class BorderlineFrameQA(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            result = await super().generate_json(system, context, schema, images=images)
            if schema is QAResult and context["stage"] == "keyframes":
                result.character_consistency = 0.82  # Fails high, passes standard.
                result.explanation = "Slight fur detail mismatch; otherwise the targets match."
            return result

    app.state.generation.provider_factory = BorderlineFrameQA
    before = preview(system, target_duration=1, quality="high", max_retries=0, qa_enabled=True)
    assert (
        client.post(
            f"/api/v1/episodes/{before['id']}/approve",
            json={"expected_version": before["version"]},
        ).status_code
        == 202
    )
    failed = wait_episode(client, before["id"])
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "QA_FAILED"
    shot = failed["shots"][0]
    assert shot["qa_retry"]["pending"] and shot["qa_frame_corrections"]
    old_jobs = app.state.store.list("job", before["id"])
    old_ids = {job["id"] for job in old_jobs}
    submissions = len(comfy.prompts)

    response = change(client, failed, "standard")
    assert response.status_code == 200, response.text
    saved = response.json()
    saved_shot = saved["shots"][0]
    assert not saved_shot.get("qa_retry") and not saved_shot.get("qa_frame_corrections")
    assert saved_shot["error"] == shot["error"]
    history = saved["generation_settings_history"][-1]["shot_execution"][shot["id"]]
    for field in ("qa_retry", "qa_frame_corrections", "render_cursor"):
        assert history[field] == shot.get(field)
    assert len(comfy.prompts) == submissions

    assert client.post(f"/api/v1/episodes/{before['id']}/generate").status_code == 202
    final = wait_episode(client, before["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    for field in ("prompts", "start_frame_asset_id", "end_frame_asset_id"):
        assert final["shots"][0][field] == shot[field]
    for field in ("plan", "bible", "references"):
        assert final[field] == failed[field]
    new_jobs = [
        job for job in app.state.store.list("job", before["id"]) if job["id"] not in old_ids
    ]
    assert [job["type"] for job in new_jobs] == ["SHOT_VIDEO"]
    assert all(app.state.store.get("job", job["id"]) == job for job in old_jobs)


@pytest.mark.parametrize(
    "state", ["PLANNING", "RENDERING_VIDEO", "busy", "QUEUED_JOB", "RUNNING_JOB", "UNKNOWN_JOB"]
)
def test_quality_change_blocks_active_and_unsettled_work(system, state):
    client, app, comfy = system
    item = create(client, quality="high")
    id = item["id"]
    if state == "busy":
        app.state.generation.busy.add(id)
    elif state.endswith("_JOB"):
        app.state.store.create("job", {"status": state.removesuffix("_JOB")}, parent=id)
    else:
        item = app.state.store.update("episode", id, {"status": state})
    try:
        response = change(client, item, "standard")
        assert response.status_code == 409, response.text
        assert app.state.store.get("episode", id) == item and not comfy.calls
    finally:
        app.state.generation.busy.discard(id)


@pytest.mark.parametrize(
    "extra", [{"quality": "ultra"}, {"quality": None}, {"qa_enabled": False}, {"plan": {}}]
)
def test_quality_request_validation_cannot_mutate_story_or_other_settings(system, extra):
    client, app, _ = system
    item = create(client, quality="high")
    response = client.patch(
        f"/api/v1/episodes/{item['id']}/quality",
        json={"expected_version": item["version"], "quality": "standard", **extra},
    )
    assert response.status_code == 422
    assert app.state.store.get("episode", item["id"]) == item


def test_stale_version_rejected_and_same_quality_is_noop(system):
    client, app, _ = system
    item = create(client, quality="high", max_retries=5)
    same = change(client, item, "high")
    assert same.status_code == 200 and same.json() == item
    current = app.state.store.update("episode", item["id"], {"title": "User edit"})
    assert change(client, item, "standard").status_code == 409
    assert app.state.store.get("episode", item["id"]) == current
