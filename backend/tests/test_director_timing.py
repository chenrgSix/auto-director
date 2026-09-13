"""Timing contracts must influence real model requests and preserve existing timelines."""

import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app.agents.directing import Directors, normalize_plan
from app.agents.provider import LLMProvider
from app.core.config import Settings
from app.core.errors import AppError
from tests.fakes import FakeProvider
from tests.media_assertions import assert_full_duration
from tests.test_agents import plan
from tests.test_api_pipeline import wait_episode


def weighted(durations):
    candidate = plan(len(durations))
    for shot, duration in zip(candidate.shots, durations, strict=True):
        shot.duration = duration
    candidate.target_duration = sum(durations)
    return candidate


@pytest.mark.parametrize(
    ("durations", "total", "minimum", "maximum", "expected"),
    [
        ([2.5] * 4, 10, 1, 3, [2.5] * 4),
        ([3, 2.5, 2.5, 2], 10, 1, 3, [3, 2.5, 2.5, 2]),
        ([1, 2, 3], 12, 1, 8, [2, 4, 6]),
        ([1, 2, 30], 10, 1, 5, [1.67, 3.33, 5]),
        ([1, 2, 30], 7, 1, 4, [1, 2, 4]),
        ([1, 1, 4], 9, 2.5, 4, [2.5, 2.5, 4]),
        ([3] * 3, 10, 1, 5, [3.34, 3.33, 3.33]),
        ([3] * 3, 3, 1, 5, [1, 1, 1]),
        ([3] * 3, 15, 1, 5, [5, 5, 5]),
        ([3] * 4, 4.6, 1.15, 1.15, [1.15] * 4),
    ],
)
def test_allocation_preserves_proportions_and_bounds(durations, total, minimum, maximum, expected):
    candidate = weighted(durations)
    before = deepcopy(candidate)
    result = normalize_plan(candidate, total, maximum, minimum=minimum)
    assert [shot.duration for shot in result.shots] == expected
    assert sum(Decimal(str(shot.duration)) for shot in result.shots) == Decimal(str(total))
    assert candidate == before


@pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]])
def test_narrative_order_does_not_change_weighted_allocation(order):
    durations = [1, 2, 4, 8]
    baseline = normalize_plan(weighted(durations), 12, 5)
    result = normalize_plan(weighted([durations[i] for i in order]), 12, 5)
    for shot, original_index in zip(result.shots, order, strict=True):
        assert shot.duration == pytest.approx(baseline.shots[original_index].duration, abs=0.01)


class TimingRecorder(FakeProvider):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def generate_json(self, system, context, schema, *, images=None):
        self.requests.append((system, deepcopy(context), schema.model_json_schema()))
        return await super().generate_json(system, context, schema, images=images)


def episode(total, **extra):
    return {
        "idea": "A hen lays an egg",
        "target_duration": total,
        "aspect_ratio": "9:16",
        "style": "自然纪录片",
        **extra,
    }


@pytest.mark.parametrize(
    ("total", "maximum", "minimum", "counts"),
    [
        (10, 3, 1, (4, 10)),
        (10, 5, 1, (2, 10)),
        (1, 5, 1, (1, 1)),
        (3.01, 3, 1, (2, 3)),
        (4, 2, 1, (2, 4)),
        (1.5, 5, 1, (1, 1)),
    ],
)
async def test_prompt_context_and_schema_share_feasible_timing(total, maximum, minimum, counts):
    provider = TimingRecorder()
    result = await Directors(provider).plan(episode(total), maximum)
    system, context, schema = provider.requests[0]
    assert f"each shot must last {minimum:g} to {maximum:g} seconds" in system
    assert f"Return {counts[0]} to {counts[1]} shots" in system
    assert context["min_shot_duration"] == minimum
    assert (context["min_shots"], context["max_shots"]) == counts
    assert "recommended_shots" not in context
    assert "recommended_shot_duration" not in context
    assert "not a recommended count" in system
    assert schema["properties"]["shots"]["minItems"] == counts[0]
    assert schema["properties"]["shots"]["maxItems"] == counts[1]
    duration = schema["$defs"]["TimedShotPlan"]["properties"]["duration"]
    assert duration["minimum"] == minimum and duration["maximum"] == maximum
    assert all(minimum <= shot.duration <= maximum for shot in result.shots)
    assert sum(s.duration for s in result.shots) == pytest.approx(total)
    if total == 10 and maximum == 3:
        assert [s.duration for s in result.shots] == [2.5] * 4


async def test_fixed_user_duration_overrides_planning_preference():
    provider = TimingRecorder()
    result = await Directors(provider).plan(episode(5, fixed_shot_duration=1), 3)
    assert [s.duration for s in result.shots] == [1] * 5
    _, context, _ = provider.requests[0]
    assert context["min_shot_duration"] == context["max_shot_duration"] == 1
    assert context["min_shots"] == context["max_shots"] == 5
    assert "user explicitly fixed every shot's duration" in provider.requests[0][0]


@pytest.mark.parametrize(("total", "maximum"), [(60, 5), (90, 5), (600, 2.5), (59.8, 1.15)])
async def test_every_batch_has_constraints_and_keeps_total(total, maximum):
    provider = TimingRecorder()
    result = await Directors(provider).plan(episode(total), maximum)
    assert len(result.shots) <= 240
    assert sum(Decimal(str(s.duration)) for s in result.shots) == Decimal(str(total))
    for system, context, _ in provider.requests:
        assert f"Return {context['min_shots']} to {context['max_shots']} shots" in system
        assert context["min_shots"] <= context["max_shots"] <= 12
        assert 1 <= context["min_shot_duration"] <= context["max_shot_duration"] <= maximum
        assert (
            context["min_shots"] * context["min_shot_duration"] <= context["target_duration"] + 1e-9
        )


@pytest.mark.parametrize("bad", [[3] * 3, [0.99, 3, 3, 3], [3.01, 2.5, 2.5, 2.5], [1] * 11])
async def test_real_provider_rejects_out_of_contract_json_then_repairs(bad):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        result = weighted(bad if len(calls) == 1 else [2.5] * 4)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handler)
    )
    result = await Directors(provider).plan(episode(10), 3)
    assert len(calls) == 2
    instruction = calls[0]["messages"][0]["content"]
    assert '"minimum": 1.0' in instruction and '"maxItems": 10' in instruction
    assert "each shot must last 1 to 3 seconds" in instruction
    assert "previous response did not match the schema" in calls[1]["messages"][-1]["content"]
    assert [s.duration for s in result.shots] == [2.5] * 4


async def test_repeated_invalid_timing_fails_instead_of_accepting_out_of_range_shots():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": weighted([0.9] * 11).model_dump_json()}}]},
        )

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handler)
    )
    with pytest.raises(AppError) as caught:
        await Directors(provider).plan(episode(10), 3)
    assert caught.value.code == "LLM_INVALID_OUTPUT"
    assert count == 2


def test_low_memory_pipeline_uses_configured_five_second_shots_and_composes_ten_seconds(system):
    client, app, comfy = system
    created = client.post("/api/v1/episodes", json={**episode(10), "memory_mode": "low"}).json()
    id = created["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    finished = wait_episode(client, id)
    assert finished["status"] == "COMPLETED", finished.get("error")
    assert [s["duration"] for s in finished["shots"]] == [5] * 2
    assert_full_duration(app, finished)
    jobs = [j for j in app.state.store.list("job", id) if j["type"] == "SHOT_VIDEO"]
    assert len(jobs) == 2
    assert all(j["input_values"]["timeline_duration"] == 5 for j in jobs)
    for job in jobs:
        graph = comfy.prompts[job["comfy_prompt_id"]]["prompt"]
        binding = job["profile_snapshot"]["bindings"]["duration"]
        assert graph[binding["node_id"]]["inputs"][binding["input"]] == 81


def test_existing_short_plan_is_resumed_without_replanning(system, monkeypatch):
    client, app, _ = system
    created = client.post("/api/v1/episodes", json=episode(10)).json()
    id = created["id"]
    previous = weighted([2.21, 1.95, 1.57, 1.65, 1.4, 1.22]).model_dump(mode="json")
    previous["target_duration"] = 10
    shots = [
        {
            **shot,
            "id": f"saved-{i}",
            "enabled": True,
            "status": "PENDING",
            "prompts": None,
            "start_frame_asset_id": None,
            "end_frame_asset_id": None,
            "video_asset_id": None,
            "actual_end_frame_asset_id": None,
            "qa": [],
            "retry_version": 0,
            "error": None,
        }
        for i, shot in enumerate(previous["shots"])
    ]
    app.state.store.update("episode", id, {"status": "FAILED", "plan": previous, "shots": shots})

    async def forbidden(*args, **kwargs):
        raise AssertionError("An existing timeline must not be replanned")

    monkeypatch.setattr(Directors, "plan", forbidden)
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    finished = wait_episode(client, id)
    assert finished["status"] == "COMPLETED", finished.get("error")
    assert finished["plan"] == previous
    assert [(s["id"], s["duration"]) for s in finished["shots"]] == [
        (s["id"], s["duration"]) for s in shots
    ]
    assert_full_duration(app, finished)
