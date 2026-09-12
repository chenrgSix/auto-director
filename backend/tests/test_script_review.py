"""Audit the script before spending on assets; corrections and recovery stay bounded."""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agents.directing import Directors
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts
from app.agents.script_review import ScriptReview, review_schema, review_script
from app.core.config import Settings
from app.core.errors import AppError
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_cancellation import wait_idle
from tests.test_episode_preview import edits, preview, wait_preview
from tests.test_episode_workflows import imported, rebind


def response(*, fix=True, issue=False):
    return {
        "summary": "修正首镜起始状态，让同一动作的起终点衔接。"
        if fix
        else "未发现明确的剧本问题。",
        "fixes": [
            {
                "shot_index": 0,
                "reason": "起点应与镜头中的行走动作匹配。",
                "changes": {"start_state": "REVIEWED: lion begins at the left edge"},
            }
        ]
        if fix
        else [],
        "issues": [
            {
                "shot_index": 0,
                "message": "短镜无法容纳原稿中的全部连续动作。",
                "suggestion": "检查动作负担，必要时调整分镜时长。",
            }
        ]
        if issue
        else [],
    }


class Reviewer(FakeProvider):
    def __init__(self, data=None):
        super().__init__()
        self.data = response() if data is None else data
        self.review_contexts = []
        self.calls = []

    async def generate_json(self, system, context, schema, *, images=None):
        self.calls.append(schema.__name__)
        if issubclass(schema, ScriptReview):
            assert images is None
            self.usage["calls"] += 1
            self.review_contexts.append((system, deepcopy(context)))
            # Exercise the application boundary even for providers that skip schema validation.
            return SimpleNamespace(model_dump=lambda: deepcopy(self.data))
        result = await super().generate_json(system, context, schema, images=images)
        if issubclass(schema, ShotPrompts):
            return schema.model_validate(
                {**result.model_dump(), "start_frame_prompt": context["shot"]["start_state"]}
            )
        return result


def install(app, provider):
    app.state.generation.provider_factory = lambda: provider


@pytest.fixture
def plan():
    return {
        "title": "test",
        "logline": "walk",
        "target_duration": 5,
        "shots": [
            {
                "index": 0,
                "title": "walk",
                "duration": 5,
                "purpose": "introduce",
                "action": "walk",
                "camera": "wide",
                "start_state": "start",
                "end_state": "end",
                "transition_from_previous": "CUT",
            }
        ],
    }


@pytest.mark.parametrize(
    "case",
    [
        "duration",
        "count",
        "unknown_field",
        "missing_lists",
        "bad_index",
        "bool_index",
        "duplicate",
        "unchanged",
        "blank",
        "empty_patch",
        "first_continuation",
        "bad_issue",
        "state",
    ],
)
def test_review_schema_rejects_unbounded_or_inconsistent_revisions(plan, case):
    data = response()
    fix = data["fixes"][0]
    if case == "duration":
        fix["changes"]["duration"] = 1
    if case == "count":
        data["shots"] = []
    if case == "unknown_field":
        fix["changes"]["workflow_id"] = "new"
    if case == "missing_lists":
        data.pop("issues")
    if case == "bad_index":
        fix["shot_index"] = 1
    if case == "bool_index":
        fix["shot_index"] = True
    if case == "duplicate":
        data["fixes"].append(deepcopy(fix))
    if case == "unchanged":
        fix["changes"]["start_state"] = "start"
    if case == "blank":
        fix["changes"]["action"] = "   "
    if case == "empty_patch":
        fix["changes"] = {}
    if case == "first_continuation":
        fix["changes"]["transition_from_previous"] = "CONTINUE_FRAME"
    if case == "bad_issue":
        data["issues"] = [{"shot_index": 9, "message": "bad", "suggestion": "bad"}]
    if case == "state":
        fix["changes"]["video_asset_id"] = "replace"
    with pytest.raises(ValidationError):
        review_schema(plan).model_validate(data)


@pytest.mark.parametrize("correct_on_retry", [True, False])
async def test_real_provider_serializes_review_schema_and_repairs_invalid_output(
    plan, correct_on_retry
):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        data = response()
        if len(calls) == 1 or not correct_on_retry:
            data["fixes"][0]["changes"]["duration"] = 20
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handle)
    )
    episode = {
        "plan": plan,
        "idea": "walk",
        "target_duration": 5,
        "aspect_ratio": "16:9",
        "style": "film",
    }
    operation = review_script(
        Directors(provider),
        episode,
        {"capability": "TEXT_TO_IMAGE"},
        {"capability": "IMAGE_TO_VIDEO"},
        {"max_duration": 5},
        {},
    )
    if correct_on_retry:
        assert (await operation).fixes[0].shot_index == 0
    else:
        with pytest.raises(AppError) as error:
            await operation
        assert error.value.code == "LLM_INVALID_OUTPUT"
    assert len(calls) == 2
    instruction = calls[0]["messages"][0]["content"]
    assert "ScopedScriptReview" in instruction and "independent script editor" in instruction
    assert "ENTIRE plan" in instruction and "reasonable quick inserts" in instruction


def test_review_precedes_bible_and_media_and_corrected_target_reaches_patch(system):
    client, app, comfy = system
    provider = Reviewer()
    install(app, provider)
    episode = preview(system, target_duration=5, max_shot_duration=5)
    id = episode["id"]
    report = episode["script_review"]
    assert (
        report["status"] == "corrected" and report["fixes"][0]["before"]["start_state"] == "start"
    )
    assert report["original_plan"]["shots"][0]["start_state"] == "start"
    assert episode["plan"]["shots"][0]["start_state"].startswith("REVIEWED:")
    assert episode["shots"][0]["prompts"]["start_frame_prompt"].startswith("REVIEWED:")
    assert not comfy.prompts and not app.state.store.list("asset", id)
    assert len(provider.review_contexts) == 1
    audit_index = provider.calls.index("ScopedScriptReview")
    assert audit_index < next(i for i, n in enumerate(provider.calls) if "VisualBible" in n)
    _, context = provider.review_contexts[0]
    assert "bible" not in context and "qa" not in context and "prompts" not in str(context["plan"])
    assert context["constraints"]["max_shot_duration"] == 5
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": episode["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["script_review"] == report and len(provider.review_contexts) == 1
    job = next(j for j in app.state.store.list("job", id) if j["type"] == "SHOT_START_FRAME")
    assert "REVIEWED:" in job["patched_workflow"]["positive"]["inputs"]["text"]
    assert final["plan"]["shots"][0]["duration"] == 5


@pytest.mark.parametrize("preview_required", [True, False])
def test_unresolved_issues_force_preview_and_explicit_ack_before_any_render(
    system, preview_required
):
    client, app, comfy = system
    provider = Reviewer(response(issue=True))
    install(app, provider)
    created = client.post(
        "/api/v1/episodes",
        json={
            "idea": "review before spending",
            "target_duration": 1,
            "preview_required": preview_required,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
        },
    ).json()
    id = created["id"]
    assert (
        client.post(
            f"/api/v1/episodes/{id}/{'preview' if preview_required else 'generate'}"
        ).status_code
        == 202
    )
    episode = wait_preview(client, id)
    assert episode["status"] == "AWAITING_REVIEW" and episode["preview_required"]
    assert episode["script_review"]["status"] == "needs_attention"
    assert not comfy.prompts and not app.state.store.list("asset", id)
    denied = client.post(
        f"/api/v1/episodes/{id}/approve", json={"expected_version": episode["version"]}
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "SCRIPT_REVIEW_REQUIRED"
    assert app.state.store.get("episode", id) == episode
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve",
            json={"expected_version": episode["version"], "accept_script_review": "yes"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve",
            json={"expected_version": episode["version"], "accept_script_review": True},
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["script_review"]["acknowledged_at"] and len(provider.review_contexts) == 1


@pytest.mark.parametrize("error_code", ["LLM_TIMEOUT", "LLM_INVALID_OUTPUT"])
def test_review_failure_keeps_draft_and_retry_resumes_only_review(system, error_code):
    client, app, comfy = system
    provider = Reviewer()
    original = provider.generate_json
    failed = False

    async def fail_once(system, context, schema, *, images=None):
        nonlocal failed
        if issubclass(schema, ScriptReview) and not failed:
            failed = True
            raise AppError(error_code, "review interrupted", status=502)
        return await original(system, context, schema, images=images)

    provider.generate_json = fail_once
    install(app, provider)
    created = client.post(
        "/api/v1/episodes",
        json={"idea": "retry audit", "target_duration": 1, "preview_required": True},
    ).json()
    id = created["id"]
    client.post(f"/api/v1/episodes/{id}/preview")
    before = wait_preview(client, id)
    assert (
        before["status"] == "FAILED"
        and before["plan"]
        and before["script_review"]["status"] == "pending"
    )
    assert before["bible"] is None and not comfy.prompts
    plan_calls = len([n for n in provider.calls if "EpisodePlan" in n])
    client.post(f"/api/v1/episodes/{id}/preview")
    after = wait_preview(client, id)
    assert after["status"] == "AWAITING_REVIEW", after.get("error")
    assert after["script_review"]["original_plan"] == before["plan"]
    assert [s["id"] for s in before["shots"]] == [s["id"] for s in after["shots"]]
    assert len([n for n in provider.calls if "EpisodePlan" in n]) == plan_calls


def test_noop_save_does_not_invalidate_review_but_user_edit_marks_it(system):
    client, app, _ = system
    provider = Reviewer(response(fix=False))
    install(app, provider)
    before = preview(system, target_duration=1)
    id = before["id"]
    same = client.patch(f"/api/v1/episodes/{id}/preview", json=edits(before)).json()
    assert not same["script_review"]["edited_after_review"]
    body = edits(same)
    body["shots"][0]["video_prompt"] = "My changed video target"
    result = client.patch(f"/api/v1/episodes/{id}/preview", json=body)
    assert result.status_code == 200, result.text
    after = result.json()
    assert after["script_review"]["edited_after_review"]
    assert after["script_review"]["original_plan"] == before["script_review"]["original_plan"]
    assert len(provider.review_contexts) == 1


def test_legacy_prepared_plan_is_not_reaudited_or_rewritten(system):
    client, app, _ = system
    before = preview(system, target_duration=1)
    id = before["id"]
    before = app.state.store.update("episode", id, lambda e: e.pop("script_review"))

    class Forbidden(FakeProvider):
        async def generate_json(self, *args, **kwargs):
            raise AssertionError("Old reviewed prompts must not invoke an agent")

    app.state.generation.provider_factory = Forbidden
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": before["version"]}
        ).status_code
        == 202
    )
    after = wait_episode(client, id)
    assert after["status"] == "COMPLETED", after.get("error")
    assert after["plan"] == before["plan"] and "script_review" not in after


def test_workflow_switch_keeps_original_review_without_a_second_pass(system):
    client, app, _ = system
    provider = Reviewer()
    install(app, provider)
    before = preview(system, target_duration=1)
    workflow = imported(app, "IMAGE_TO_VIDEO")
    changed = rebind(client, before, video_workflow_id=workflow["id"])
    assert changed.status_code == 200, changed.text
    after = changed.json()
    assert after["plan"] == before["plan"] and after["script_review"] == before["script_review"]
    assert (
        after["workflow_binding_history"][-1]["previous_state"]["script_review"]
        == before["script_review"]
    )
    assert len(provider.review_contexts) == 1


def test_restart_during_review_keeps_plan_and_resumes_pending_audit(system):
    client, app, comfy = system
    entered = threading.Event()
    provider = Reviewer()
    original = provider.generate_json
    block = True

    async def pause(system, context, schema, *, images=None):
        if issubclass(schema, ScriptReview) and block:
            entered.set()
            await asyncio.Event().wait()
        return await original(system, context, schema, images=images)

    provider.generate_json = pause
    install(app, provider)
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "restart audit", "target_duration": 1, "preview_required": True},
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/preview")
    assert entered.wait(3)
    before = app.state.store.get("episode", id)
    assert before["status"] == "REVIEWING_SCRIPT"
    client.portal.call(app.state.generation.stop)
    wait_idle(app.state.generation, id)
    block = False
    client.portal.call(app.state.generation.start)
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    after = wait_preview(client, id)
    assert after["status"] == "AWAITING_REVIEW", after.get("error")
    assert after["script_review"]["original_plan"] == before["plan"]
    assert [s["id"] for s in before["shots"]] == [s["id"] for s in after["shots"]]
    assert not comfy.prompts


def test_custom_provider_cannot_apply_invalid_fix_or_begin_bible(system):
    client, app, comfy = system
    data = response()
    data["fixes"][0]["changes"]["duration"] = 10
    provider = Reviewer(data)
    install(app, provider)
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "invalid audit", "target_duration": 1, "preview_required": True},
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/preview")
    result = wait_preview(client, id)
    assert result["status"] == "FAILED" and result["error"]["code"] == "LLM_INVALID_OUTPUT"
    assert result["script_review"]["status"] == "pending" and result["bible"] is None
    assert (
        result["shots"][0]["duration"] == 1 and result["plan"]["shots"][0]["start_state"] == "start"
    )
    assert not comfy.prompts


@pytest.mark.parametrize("duration", [60, 90])
def test_long_film_is_reviewed_as_one_complete_story_after_all_plan_batches(system, duration):
    _, app, comfy = system
    provider = Reviewer(response(fix=False))
    install(app, provider)
    episode = preview(system, target_duration=duration, max_shot_duration=5)
    assert len(provider.review_contexts) == 1
    context = provider.review_contexts[0][1]
    assert len(context["plan"]["shots"]) == len(episode["shots"])
    assert sum(s["duration"] for s in context["plan"]["shots"]) == duration
    assert context["plan"]["shots"][-1]["index"] == len(episode["shots"]) - 1
    assert not comfy.prompts


def test_successful_audit_is_reused_after_bible_failure(system):
    client, app, comfy = system
    provider = Reviewer()
    original = provider.generate_json
    failed = False

    async def fail_bible_once(system, context, schema, *, images=None):
        nonlocal failed
        if "VisualBible" in schema.__name__ and not failed:
            failed = True
            raise AppError("LLM_TIMEOUT", "Bible timeout", status=504)
        return await original(system, context, schema, images=images)

    provider.generate_json = fail_bible_once
    install(app, provider)
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "reuse audit", "target_duration": 1, "preview_required": True},
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/preview")
    before = wait_preview(client, id)
    assert before["status"] == "FAILED" and before["script_review"]["status"] == "corrected"
    client.post(f"/api/v1/episodes/{id}/preview")
    after = wait_preview(client, id)
    assert after["status"] == "AWAITING_REVIEW", after.get("error")
    assert len(provider.review_contexts) == 1
    assert after["script_review"] == before["script_review"]
    assert not comfy.prompts
