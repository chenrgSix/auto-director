"""The Director may choose different rhythms without changing the timing contract."""

import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app.agents.directing import Directors
from app.agents.provider import LLMProvider
from app.agents.schemas import EpisodePlan, Transition
from app.core.config import Settings
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_director_timing import episode, weighted
from tests.test_episode_preview import edits, preview

STORY_RHYTHM = [3, 4, 5, 3, 4, 5, 3, 3]


def narrative_plan(durations, context):
    result = weighted(durations)
    previous = context["previous_shots"]
    offset = previous[-1]["index"] + 1 if previous else 0
    for index, shot in enumerate(result.shots):
        shot.title = f"Story beat {offset + index + 1}"
        shot.purpose = f"Reveal story beat {offset + index + 1} over {shot.duration:g} seconds"
        shot.transition_from_previous = Transition.CONTINUE_FRAME if index == 0 else Transition.CUT
    return result


@pytest.mark.parametrize("total", [30, 60, 90])
@pytest.mark.parametrize("rhythm", [STORY_RHYTHM, [1, 2, 3, 4, 5] * 2, [5] * 6])
async def test_real_provider_keeps_story_chosen_count_and_durations(total, rhythm):
    requests, responses = [], []

    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"])
        assert context["target_duration"] == 30
        result = narrative_plan(rhythm, context)
        requests.append((body["messages"][0]["content"], context))
        responses.extend(result.shots)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handler)
    )
    result = await Directors(provider).plan(episode(total), 5)
    assert len(requests) == total // 30  # No repair/retry for any of these narrative choices.
    assert [s.duration for s in result.shots] == rhythm * (total // 30)
    assert [s.purpose for s in result.shots] == [s.purpose for s in responses]
    assert [s.index for s in result.shots] == list(range(len(result.shots)))
    assert sum(Decimal(str(s.duration)) for s in result.shots) == Decimal(total)
    offset = 0
    for index, (system, context) in enumerate(requests):
        assert "not a recommended count" in system
        assert "a ceiling, not a target" in system
        assert "recommended_shots" not in context
        assert context["min_shots"] < context["max_shots"]
        assert context["start_time"] == index * 30
        assert context["previous_shots"] == [
            s.model_dump(mode="json") for s in result.shots[max(0, offset - 3) : offset]
        ]
        if index:
            assert result.shots[offset].transition_from_previous == Transition.CONTINUE_FRAME
        offset += len(rhythm)


@pytest.mark.parametrize(("total", "maximum"), [(600, 3), (599.99, 3.2), (600, 2.5)])
async def test_dense_story_reserves_enough_shots_for_later_batches(total, maximum):
    contexts = []
    used = 0

    def handler(request):
        nonlocal used
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"])
        contexts.append(context)
        count = context["max_shots"]
        length, remainder = divmod(round(context["target_duration"] * 100), count)
        durations = [(length + (i < remainder)) / 100 for i in range(count)]
        used += count
        remaining = (
            Decimal(str(total))
            - Decimal(str(context["start_time"]))
            - Decimal(str(context["target_duration"]))
        )
        assert remaining <= (240 - used) * Decimal(str(maximum))
        result = narrative_plan(durations, context)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handler)
    )
    result = await Directors(provider).plan(episode(total), maximum)
    assert len(result.shots) == 240
    assert sum(Decimal(str(s.duration)) for s in result.shots) == Decimal(str(total))
    assert all(1 <= s.duration <= maximum for s in result.shots)
    assert all(c["min_shots"] <= c["max_shots"] <= 12 for c in contexts)
    if maximum == 2.5:
        # A 600-second film at this ceiling needs all 240 slots: no extra calls for headroom.
        assert len(contexts) == 20
        assert all(c["min_shots"] == c["max_shots"] == 12 for c in contexts)
    else:
        assert any(c["max_shots"] < 12 for c in contexts)


class StoryProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, EpisodePlan):
            self.usage["calls"] += 1
            return schema.model_validate(narrative_plan(STORY_RHYTHM, context).model_dump())
        return await super().generate_json(system, context, schema, images=images)


def test_preview_approval_and_render_keep_variable_story_timing(system, monkeypatch):
    client, app, _ = system
    app.state.generation.provider_factory = StoryProvider
    created = preview(system, target_duration=30, max_shot_duration=5)
    id = created["id"]
    assert [s["duration"] for s in created["shots"]] == STORY_RHYTHM
    assert not app.state.store.list("job", id)
    saved = client.patch(f"/api/v1/episodes/{id}/preview", json=edits(created))
    assert saved.status_code == 200, saved.text
    original_plan = deepcopy(saved.json()["plan"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("Approval must reuse the reviewed story")

    monkeypatch.setattr(Directors, "plan", forbidden)
    response = client.post(
        f"/api/v1/episodes/{id}/approve",
        json={"expected_version": saved.json()["version"]},
    )
    assert response.status_code == 202, response.text
    finished = wait_episode(client, id)
    assert finished["status"] == "COMPLETED", finished.get("error")
    assert finished["plan"] == original_plan
    assert [s["duration"] for s in finished["shots"]] == STORY_RHYTHM
    assert finished["final_duration"] == pytest.approx(30, abs=0.1)
    jobs = {j["shot_id"]: j for j in app.state.store.list("job", id) if j["type"] == "SHOT_VIDEO"}
    assert len(jobs) == len(STORY_RHYTHM)
    for shot in finished["shots"]:
        job = jobs[shot["id"]]
        assert job["input_values"]["timeline_duration"] == shot["duration"]
        binding = job["profile_snapshot"]["bindings"]["duration"]
        frames = job["patched_workflow"][binding["node_id"]]["inputs"][binding["input"]]
        assert frames == shot["duration"] * 16 + 1
