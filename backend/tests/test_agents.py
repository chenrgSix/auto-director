import json

import httpx
import pytest

from app.agents.directing import generation_budget, normalize_plan
from app.agents.provider import LLMProvider
from app.agents.schemas import EpisodePlan, QAResult
from app.core.config import Settings
from app.core.errors import AppError


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
    ("total", "maximum", "count"), [(5, 5, 2), (10, 3, 4), (15, 5, 5), (29.8, 3.2, 10)]
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
