from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.agents.directing import Directors
from app.agents.schemas import QAResult
from app.core.errors import AppError


def result_data(**extra):
    return {
        "character_consistency": 0.9,
        "scene_consistency": 0.9,
        "style_consistency": 0.9,
        "action_accuracy": 0.9,
        "transition_quality": 0.9,
        "artifact_score": 0.1,
        "explanation": "The supplied endpoints match their reviewed targets.",
        **extra,
    }


def test_legacy_qa_output_remains_valid_without_frame_feedback():
    result = QAResult.model_validate(result_data())
    assert result.failed_frames == []
    assert result.frame_corrections == {}
    assert result.retry_scope() is None


def test_frame_feedback_is_bounded_and_preserves_targeted_role():
    data = result_data(
        failed_frames=["end_frame"],
        frame_corrections={
            "end_frame": "  Remove the hands; retain the straw nest and dawn light.  "
        },
    )
    before = deepcopy(data)
    result = QAResult.model_validate(data)
    assert result.failed_frames == ["end_frame"]
    assert result.frame_corrections == {
        "end_frame": "Remove the hands; retain the straw nest and dawn light."
    }
    assert data == before


@pytest.mark.parametrize(
    "feedback",
    [
        {"failed_frames": ["middle_frame"]},
        {"failed_frames": ["start_frame", "start_frame"]},
        {"frame_corrections": {"end_frame": "Remove extra hands."}},
        {"failed_frames": ["end_frame"], "frame_corrections": {"start_frame": "Wrong role"}},
        {"failed_frames": ["end_frame"], "frame_corrections": {"end_frame": "  "}},
        {"failed_frames": ["end_frame"], "frame_corrections": {"end_frame": "x" * 1501}},
        {"failed_frames": ["end_frame"], "frame_corrections": {"end_frame": 123}},
    ],
)
def test_invalid_frame_feedback_is_rejected(feedback):
    with pytest.raises(ValidationError):
        QAResult.model_validate(result_data(**feedback))


class RecordingQAProvider:
    def __init__(self, **feedback):
        self.feedback = feedback
        self.calls = []

    async def generate_json(self, system, context, schema, *, images=None):
        self.calls.append((system, context, images))
        return schema.model_validate(result_data(**self.feedback))


@pytest.mark.parametrize(
    ("stage", "roles"),
    [
        ("start_candidate", ["start_frame"]),
        ("keyframes", ["start_frame"]),
        ("keyframes", ["start_frame", "end_frame"]),
        ("video", ["start_frame", "middle_frame", "end_frame"]),
        ("video", ["start_frame", "middle_frame", "end_frame", "previous_last_frame"]),
    ],
)
async def test_qa_frame_order_matches_supplied_capability_and_stage(tmp_path, stage, roles):
    provider = RecordingQAProvider()
    paths = [tmp_path / f"{role}.png" for role in roles]
    await Directors(provider).qa({}, {"prompts": {}}, paths, stage)
    system, context, sent_paths = provider.calls[0]
    assert context["frame_order"] == roles
    assert sent_paths == paths
    assert "must list only end_frame" in system
    assert "For video leave these fields empty" in system


@pytest.mark.parametrize("stage", ["keyframes", "start_candidate", "video"])
async def test_qa_rejects_unavailable_end_frame_feedback(tmp_path, stage):
    provider = RecordingQAProvider(
        failed_frames=["end_frame"], frame_corrections={"end_frame": "Remove the hands."}
    )
    paths = [tmp_path / "start.png"]
    with pytest.raises(AppError) as caught:
        await Directors(provider).qa({}, {"prompts": {}}, paths, stage)
    assert caught.value.code == "LLM_INVALID_OUTPUT"
