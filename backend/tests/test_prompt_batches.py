import asyncio
import json
import math
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agents.directing import Directors
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts
from app.agents.shot_batch import (
    ShotPromptBatch,
    batch_context,
    prompt_batch_schema,
    select_prompt_batch,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.core.runtime_settings import RuntimeSettings, SettingsPatch
from app.db.store import Store
from app.generation.parameters import ai_parameters
from app.generation.pipeline import GenerationService
from app.generation.prompt_preparation import preparation_progress, prepare_prompts
from tests.fakes import FakeProvider
from tests.test_ai_parameter_constraints import linked_ai_profile
from tests.test_ai_parameters import specifications


def shots(durations):
    return [
        {
            "id": f"shot-{i}",
            "index": i,
            "title": f"Walk {i}",
            "duration": d,
            "purpose": "explore",
            "action": "walk",
            "camera": "wide",
            "start_state": "start",
            "end_state": "end",
            "transition_from_previous": "CUT",
            "enabled": True,
            "prompts": None,
            "status": "PENDING",
        }
        for i, d in enumerate(durations)
    ]


def preparation(tmp_path, durations, maximum=3):
    store = Store(tmp_path)
    episode = store.create(
        "episode",
        {
            "status": "PREPARING_PROMPTS",
            "idea": "A lion walks",
            "bible": {},
            "shots": shots(durations),
            "plan": {"title": "walking"},
            "references": {"kept": "asset-old"},
            "final_video_asset_id": "old-video",
        },
    )

    def check_cancel(id):
        if store.get("episode", id)["status"] == "CANCELLED":
            raise AppError("CANCELLED", "cancelled")

    return SimpleNamespace(
        store=store, settings=SimpleNamespace(prompt_batch_size=maximum), check_cancel=check_cancel
    ), episode["id"]


class TrackingProvider(FakeProvider):
    def __init__(self, hook=None):
        super().__init__()
        self.requests, self.request_count, self.hook = [], 0, hook

    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, ShotPromptBatch) or "shots" not in context:
            self.request_count += 1
            self.requests.append(deepcopy(context))
            if self.hook:
                self.hook(self.request_count)
        result = await super().generate_json(system, context, schema, images=images)
        if issubclass(schema, ShotPrompts):
            result.continuity_state = {"lion_a": f"end of {context['shot']['id']}"}
        return result


async def output(durations, specs=None):
    items = shots(durations)
    schema = prompt_batch_schema(items, specs or [])
    response = await FakeProvider().generate_json(
        "", batch_context({}, items, {}, specs or [], ""), schema
    )
    return schema, response.model_dump(mode="json")


@pytest.mark.parametrize("durations", [[5, 4, 2], [1, 3.5], [4, 5, 6]])
async def test_batch_schema_round_trips_ids_order_and_each_shots_own_timing(durations):
    schema, data = await output(durations)
    validated = schema.model_validate_json(json.dumps(data))
    assert [item.shot_id for item in validated.shots] == [s["id"] for s in shots(durations)]
    provider = TrackingProvider()
    result = await Directors(provider).shot_batch({}, shots(durations), {"lion_a": "injured"})
    assert provider.request_count == 1
    assert provider.requests[0]["continuity"] == {"lion_a": "injured"}
    for duration, prompt in zip(durations, result, strict=True):
        assert prompt.video_prompt.count("Shot timing (seconds):") == (duration >= 4)


@pytest.mark.parametrize(
    "mutation", ["reverse", "duplicate", "missing", "extra", "unknown", "wrong_duration", "gap"]
)
async def test_batch_rejects_partial_reordered_and_invalid_timing_outputs(mutation):
    schema, data = await output([5, 4, 2])
    if mutation == "reverse":
        data["shots"].reverse()
    elif mutation == "duplicate":
        data["shots"][1]["shot_id"] = "shot-0"
    elif mutation == "missing":
        data["shots"].pop()
    elif mutation == "extra":
        data["shots"].append(deepcopy(data["shots"][0]))
    elif mutation == "unknown":
        data["shots"][0]["shot_id"] = "not-requested"
    elif mutation == "wrong_duration":
        data["shots"][1]["prompts"]["action_beats"][-1]["end"] = 5
    else:
        data["shots"][0]["prompts"]["action_beats"][1]["start"] += 0.1
    with pytest.raises(ValidationError):
        schema.model_validate(data)


@pytest.mark.parametrize(
    "mapping",
    [
        {"other": {"custom.amount": 0.5}},
        {"workflow-a": {"unknown": 1}},
        {"workflow-a": {"custom.amount": "0.5"}},
        {"workflow-a": {"custom.amount": -1}},
        {"workflow-a": {"custom.amount": 2}},
        {"workflow-a": {"custom.count": True}},
        {"workflow-a": {"custom.mode": "unknown"}},
        {"workflow-a": {"custom.note": "x" * 20001}},
    ],
)
async def test_each_batch_item_enforces_ai_owner_types_bounds_and_enum(mapping):
    schema, data = await output([5, 2], specifications())
    data["shots"][1]["prompts"]["ai_parameters"] = mapping
    with pytest.raises(ValidationError):
        schema.model_validate(data)


async def test_batch_preserves_nested_downstream_step_constraints():
    schema, data = await output([5, 2], ai_parameters(linked_ai_profile()))
    for value in [16, 144]:
        data["shots"][0]["prompts"]["ai_parameters"] = {"linked-ai": {"source.value": value}}
        schema.model_validate(data)
    for value in [64, 80, 16.0, True]:
        data["shots"][0]["prompts"]["ai_parameters"] = {"linked-ai": {"source.value": value}}
        with pytest.raises(ValidationError):
            schema.model_validate(data)


@pytest.mark.parametrize("repair", [True, False])
async def test_http_batch_has_one_correction_budget_and_valid_serialized_schema(repair):
    _, data = await output([5, 4, 2])
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        body = deepcopy(data)
        if len(calls) == 1 or not repair:
            body["shots"].pop()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(respond)
    )
    if repair:
        assert len(await Directors(provider).shot_batch({}, shots([5, 4, 2]), {})) == 3
    else:
        with pytest.raises(AppError, match="两次"):
            await Directors(provider).shot_batch({}, shots([5, 4, 2]), {})
    assert len(calls) == provider.request_count == 2
    assert "prefixItems" in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "count,maximum,expected", [(95, 3, 32), (240, 3, 80), (7, 2, 4), (5, 1, 5)]
)
async def test_long_preview_batches_preserve_story_and_sequential_checkpoints(
    tmp_path, count, maximum, expected
):
    durations = [4] * 15 + [3] * 80 if count == 95 else [2.5] * count
    service, id = preparation(tmp_path, durations, maximum)
    before = service.store.get("episode", id)
    provider = TrackingProvider()
    await prepare_prompts(service, id, Directors(provider), ())
    episode = service.store.get("episode", id)
    state = preparation_progress(episode)
    assert state["completed"] == state["total"] == count
    assert state["requests"] == expected == provider.request_count
    assert state["status"] == "completed" and len(state["recent_batches"]) <= 30
    for i, context in enumerate(provider.requests):
        previous = {} if i == 0 else {"lion_a": f"end of shot-{i * maximum - 1}"}
        assert {k: v for k, v in context["continuity"].items() if k != "scene_states"} == previous
        assert "scene_states" in context["continuity"]
    for key in ("bible", "plan", "references", "final_video_asset_id"):
        assert episode[key] == before[key]
    assert [s["duration"] for s in episode["shots"]] == durations
    assert not service.store.list("job") and not service.store.list("asset")


async def test_saved_islands_split_batches_and_supply_their_continuity(tmp_path):
    service, id = preparation(tmp_path, [2] * 9)

    def saved(current):
        for i in [1, 5]:
            current["shots"][i]["prompts"] = {
                "continuity_state": {"lion_a": f"saved-{i}"},
                "legacy": "untouched",
            }

    before = service.store.update("episode", id, saved)
    provider = TrackingProvider()
    await prepare_prompts(service, id, Directors(provider), ())
    assert [len(c.get("shots", [c.get("shot")])) for c in provider.requests] == [1, 3, 3]
    assert [
        {k: v for k, v in c["continuity"].items() if k != "scene_states"} for c in provider.requests
    ] == [
        {},
        {"lion_a": "saved-1"},
        {"lion_a": "saved-5"},
    ]
    episode = service.store.get("episode", id)
    assert all(episode["shots"][i] == before["shots"][i] for i in [1, 5])


async def test_failure_keeps_completed_batch_and_resume_requests_only_missing_shots(tmp_path):
    service, id = preparation(tmp_path, [2] * 8)

    def fail(second):
        if second == 2:
            raise AppError("LLM_RATE_LIMITED", "rate limited")

    provider = TrackingProvider(fail)
    with pytest.raises(AppError, match="rate limited"):
        await prepare_prompts(service, id, Directors(provider), ())
    partial = service.store.get("episode", id)
    assert [bool(s["prompts"]) for s in partial["shots"]] == [True] * 3 + [False] * 5
    assert partial["prompt_preparation"]["requests"] == 2
    assert partial["prompt_preparation"]["recent_batches"][-1]["error_code"] == "LLM_RATE_LIMITED"
    resumed = TrackingProvider()
    await prepare_prompts(service, id, Directors(resumed), ())
    assert resumed.request_count == 2
    assert {k: v for k, v in resumed.requests[0]["continuity"].items() if k != "scene_states"} == {
        "lion_a": "end of shot-2"
    }
    assert service.store.get("episode", id)["shots"][:3] == partial["shots"][:3]


@pytest.mark.parametrize(
    "change", ["cancel", "idea", "bible", "duration", "prompt", "run_id", "workflow"]
)
async def test_late_batch_cannot_overwrite_cancelled_or_changed_sources(tmp_path, change):
    service, id = preparation(tmp_path, [2] * 3)
    profile = service.store.create("workflow", {"parameters": [], "configuration_version": 1})

    def mutate(_):
        if change == "workflow":
            service.store.update("workflow", profile["id"], {"configuration_version": 2})
            return

        def edit(current):
            if change == "cancel":
                current["status"] = "CANCELLED"
            elif change == "idea":
                current["idea"] = "changed story"
            elif change == "bible":
                current["bible"] = {"style": "changed"}
            elif change == "duration":
                current["shots"][0]["duration"] = 1
            elif change == "prompt":
                current["shots"][0]["prompts"] = {"user": "new prompt"}
            else:
                current["prompt_preparation"]["run_id"] = "new-run"

        service.store.update("episode", id, edit)

    with pytest.raises(AppError) as error:
        await prepare_prompts(service, id, Directors(TrackingProvider(mutate)), (profile,))
    assert error.value.code == ("CANCELLED" if change == "cancel" else "PROMPT_PREPARATION_STALE")
    episode = service.store.get("episode", id)
    assert all(not s["prompts"] for s in episode["shots"][1:])
    assert episode["shots"][0]["prompts"] == (
        {"user": "new prompt"} if change == "prompt" else None
    )
    if change == "cancel":
        assert episode["status"] == "CANCELLED"
    if change == "run_id":
        assert episode["prompt_preparation"]["run_id"] == "new-run"


async def test_metadata_refresh_does_not_invalidate_preparation(tmp_path):
    service, id = preparation(tmp_path, [2] * 3)
    profile = service.store.create("workflow", {"parameters": [], "configuration_version": 1})
    provider = TrackingProvider(
        lambda _: service.store.update("workflow", profile["id"], {"validation": {"valid": True}})
    )
    await prepare_prompts(service, id, Directors(provider), (profile,))
    assert all(s["prompts"] for s in service.store.get("episode", id)["shots"])


async def test_workflow_changed_during_bible_cannot_be_blessed_at_batch_start(tmp_path):
    service, id = preparation(tmp_path, [2] * 3)
    profile = service.store.create("workflow", {"parameters": [], "configuration_version": 1})
    service.store.update("workflow", profile["id"], {"configuration_version": 2})
    provider = TrackingProvider()
    with pytest.raises(AppError) as error:
        await prepare_prompts(service, id, Directors(provider), (profile,))
    assert error.value.code == "PROMPT_PREPARATION_STALE" and not provider.requests


async def test_restart_marks_wait_interrupted_and_reuses_completed_batch(tmp_path):
    fixture, id = preparation(tmp_path, [2] * 6)

    def fail(second):
        if second == 2:
            raise AppError("LLM_TIMEOUT", "timeout")

    with pytest.raises(AppError):
        await prepare_prompts(fixture, id, Directors(TrackingProvider(fail)), ())
    partial = fixture.store.get("episode", id)
    fixture.store.update(
        "episode",
        id,
        {
            "prompt_preparation": {**partial["prompt_preparation"], "status": "running"},
        },
    )
    service = GenerationService(
        Settings(_env_file=None, data_dir=tmp_path), fixture.store, None, None
    )
    await service.start()
    try:
        interrupted = service.store.get("episode", id)
        assert interrupted["status"] == "FAILED"
        assert interrupted["prompt_preparation"]["status"] == "failed"
        assert interrupted["shots"] == partial["shots"]
        service.stage(id, "PREPARING_PROMPTS")
        provider = TrackingProvider()
        await prepare_prompts(service, id, Directors(provider), ())
        assert provider.request_count == 1
        assert service.store.get("episode", id)["shots"][:3] == partial["shots"][:3]
    finally:
        await service.stop()


async def test_illegal_custom_provider_batch_is_rejected_before_any_checkpoint(tmp_path):
    service, id = preparation(tmp_path, [2] * 3)

    class Illegal(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            result = await super().generate_json(system, context, schema, images=images)
            if issubclass(schema, ShotPromptBatch):
                data = result.model_dump(mode="json")
                data["shots"][1]["prompts"]["ai_parameters"] = {"unbound": {"model": "wrong"}}
                return SimpleNamespace(model_dump=lambda: data)
            return result

    with pytest.raises(AppError) as error:
        await prepare_prompts(service, id, Directors(Illegal()), ())
    assert error.value.code == "LLM_INVALID_OUTPUT"
    assert not any(s["prompts"] for s in service.store.get("episode", id)["shots"])


@pytest.mark.parametrize("error", ["rate_limit", "network"])
async def test_real_transport_failure_counts_one_request_without_split_retry(tmp_path, error):
    service, id = preparation(tmp_path, [2] * 3)

    def respond(request):
        if error == "network":
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(429)

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(respond)
    )
    with pytest.raises(AppError):
        await prepare_prompts(service, id, Directors(provider), ())
    state = service.store.get("episode", id)["prompt_preparation"]
    assert provider.request_count == state["requests"] == 1
    assert state["batches"] == 0 and len(state["recent_batches"]) == 1


async def test_task_cancellation_drains_model_and_saves_no_partial_batch(tmp_path):
    service, id = preparation(tmp_path, [2] * 3)
    entered, cleanup = asyncio.Event(), asyncio.Event()

    class Blocked(FakeProvider):
        async def generate_json(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()

    task = asyncio.create_task(prepare_prompts(service, id, Directors(Blocked()), ()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup.is_set()
    episode = service.store.get("episode", id)
    assert not any(s["prompts"] for s in episode["shots"])
    assert episode["prompt_preparation"]["status"] == "failed"


def test_batch_budget_limits_context_and_output_and_excludes_render_history():
    items = shots([5] * 4)
    items[0].update(qa=["secret-history"], error={"detail": "not-prompt-input"})
    assert len(select_prompt_batch({}, items, {}, [], "story", 3)) == 3
    assert len(select_prompt_batch({}, items, {}, [], "story", 2)) == 2
    assert len(select_prompt_batch({"large": "x" * 48000}, items, {}, [], "story", 3)) == 1
    large_specs = [{**specifications()[3], "key": f"large.{i}"} for i in range(4)]
    assert len(select_prompt_batch({}, items, {}, large_specs, "story", 3)) == 1
    serialized = json.dumps(batch_context({}, items, {}, [], "story"))
    assert "secret-history" not in serialized and "not-prompt-input" not in serialized


@pytest.mark.parametrize("value", [0, 4, True, "2", 2.5, None, math.nan])
def test_online_batch_size_strictly_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate({"prompt_batch_size": value})


def test_persisted_batch_size_and_legacy_settings_default(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    runtime = RuntimeSettings(settings, {})
    assert runtime.public()["prompt_batch_size"] == 3
    runtime.persist({"prompt_batch_size": 1})
    restored = RuntimeSettings(Settings(_env_file=None, data_dir=tmp_path), {})
    assert restored.public()["prompt_batch_size"] == 1
