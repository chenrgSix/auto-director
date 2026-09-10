import json

import httpx
import pytest

from app.agents.directing import Directors, generation_budget, normalize_plan
from app.agents.provider import LLMProvider
from app.agents.schemas import EpisodePlan, QAResult, Transition
from app.core.config import Settings
from app.core.errors import AppError
from tests.fakes import FakeProvider


def plan(count):
    return EpisodePlan.model_validate(
        {
            "title": "Three lions",
            "logline": "Lions explore",
            "target_duration": 10,
            "shots": [
                {
                    "index": i,
                    "title": f"Shot {i}",
                    "duration": 3,
                    "purpose": "explore",
                    "action": "walk",
                    "camera": "wide",
                    "start_state": "still",
                    "end_state": "moving",
                    "transition_from_previous": "CUT",
                }
                for i in range(count)
            ],
        }
    )


@pytest.mark.parametrize(
    ("total", "maximum", "count"),
    [(5, 5, 2), (10, 3, 4), (15, 5, 5), (29.8, 3.2, 10), (600, 5, 120), (600, 2.5, 240)],
)
def test_duration_allocation_exact_and_bounded(total, maximum, count):
    result = normalize_plan(plan(count), total, maximum)
    assert sum(shot.duration for shot in result.shots) == pytest.approx(total)
    assert all(1 <= shot.duration <= maximum for shot in result.shots)


def test_infeasible_plan_is_rejected():
    with pytest.raises(AppError):
        normalize_plan(plan(2), 15, 5)


def test_qa_thresholds_choose_targeted_retry():
    good = dict(
        character_consistency=0.9,
        scene_consistency=0.9,
        style_consistency=0.9,
        action_accuracy=0.9,
        transition_quality=0.9,
        artifact_score=0.1,
        explanation="ok",
    )
    assert QAResult(**good).retry_scope() is None
    assert QAResult(**{**good, "character_consistency": 0.4}).retry_scope() == "keyframes"
    assert QAResult(**{**good, "action_accuracy": 0.3}).retry_scope() == "video"
    assert QAResult(**{**good, "transition_quality": 0.3}).retry_scope() == "transition"


async def test_provider_repairs_schema_once_and_never_executes_extra_fields():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        content = '{"shell": "bad"}' if len(calls) == 1 else plan(2).model_dump_json()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 2}},
        )

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handler)
    )
    result = await provider.generate_json("Director", {"idea": "test"}, EpisodePlan)
    assert len(result.shots) == 2
    assert len(calls) == 2
    assert provider.usage["prompt_tokens"] == 4
    assert "tools" not in calls[0]


def test_low_vram_changes_clip_budget():
    result = generation_budget(
        {"quality": "standard", "aspect_ratio": "9:16"},
        {"max_duration": 5},
        {"devices": [{"vram_free": 6 * 1024**3}]},
    )
    assert result["max_duration"] == 3
    assert result["low_memory"]
    assert result["width"] == 384


class RecordingDirector(FakeProvider):
    def __init__(self):
        super().__init__()
        self.contexts = []

    async def generate_json(self, system, context, schema, *, images=None):
        self.contexts.append(context.copy())
        result = await super().generate_json(system, context, schema, images=images)
        result.shots[0].transition_from_previous = Transition.CONTINUE_FRAME
        return result


@pytest.mark.parametrize(
    ("total", "maximum"),
    [(1, 5), (60, 5), (90, 5), (600, 5), (600, 2.5), (239, 1), (599.99, 3.2), (59.8, 1.15)],
)
async def test_long_plans_batch_with_continuity_and_exact_timeline(total, maximum):
    provider = RecordingDirector()
    result = await Directors(provider).plan(
        {"idea": "A journey", "target_duration": total, "aspect_ratio": "9:16", "style": "film"},
        maximum,
    )
    assert result.target_duration == total
    assert sum(shot.duration for shot in result.shots) == pytest.approx(total)
    assert all(1 <= shot.duration <= maximum for shot in result.shots)
    assert [shot.index for shot in result.shots] == list(range(len(result.shots)))
    assert result.shots[0].transition_from_previous == Transition.ESTABLISHING_CUT
    elapsed, offset = 0, 0
    for index, context in enumerate(provider.contexts):
        assert context["episode_duration"] == total
        assert context["start_time"] == pytest.approx(elapsed)
        assert context["is_final_segment"] == (index == len(provider.contexts) - 1)
        assert context["min_shots"] <= context["max_shots"] <= 12
        if index:
            assert context["episode_title"] == result.title
            assert [shot["index"] for shot in context["previous_shots"]] == list(
                range(offset - 3, offset)
            )
            assert result.shots[offset].transition_from_previous == Transition.CONTINUE_FRAME
        offset += context["min_shots"]
        elapsed += context["target_duration"]
    assert elapsed == pytest.approx(total)


async def test_cancel_during_batch_prevents_next_model_request():
    provider = RecordingDirector()

    def check_cancel():
        if provider.contexts:
            raise AppError("CANCELLED", "cancelled")

    with pytest.raises(AppError, match="cancelled"):
        await Directors(provider).plan(
            {"idea": "journey", "target_duration": 600, "aspect_ratio": "9:16", "style": "film"},
            5,
            check_cancel=check_cancel,
        )
    assert len(provider.contexts) == 1
