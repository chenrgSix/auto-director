"""Model artifact severity is unambiguous while stored QA scores stay compatible."""

import json

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.agents.directing import Directors
from app.agents.provider import LLMProvider
from app.agents.schemas import QAResult
from app.core.config import Settings


def assessment(**extra):
    return {
        "character_consistency": 0.9,
        "scene_consistency": 0.95,
        "style_consistency": 0.95,
        "action_accuracy": 0.88,
        "transition_quality": 0.92,
        "explanation": "The images match the target with no significant artifacts.",
        "failed_frames": [],
        **extra,
    }


def test_model_schema_requests_severity_but_stored_schema_keeps_score():
    model_schema = QAResult.model_json_schema()
    assert "artifact_severity" in model_schema["required"]
    assert "artifact_score" not in model_schema["properties"]
    severity = model_schema["properties"]["artifact_severity"]
    assert severity["minimum"] == 0 and severity["maximum"] == 1
    assert "lower is better" in severity["description"]
    assert "0 means no visible artifacts" in severity["description"]
    stored_schema = QAResult.model_json_schema(mode="serialization")
    assert "artifact_score" in stored_schema["required"]
    assert "artifact_severity" not in stored_schema["properties"]


@pytest.mark.parametrize("input_key", ["artifact_severity", "artifact_score"])
def test_new_model_output_and_legacy_records_serialize_to_original_field(input_key):
    result = QAResult.model_validate(assessment(**{input_key: 0.1}))
    assert result.artifact_score == 0.1
    assert result.retry_scope() is None
    assert result.retry_scope(high=True) is None
    for stored in (result.model_dump(), result.model_dump(by_alias=True)):
        assert stored["artifact_score"] == 0.1
        assert "artifact_severity" not in stored
        assert QAResult.model_validate(stored) == result
    assert QAResult.model_validate_json(result.model_dump_json()) == result
    assert QAResult(**assessment(artifact_score=0.1)) == result


@pytest.mark.parametrize("input_key", ["artifact_severity", "artifact_score"])
@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_artifact_severity_keeps_original_numeric_bounds(input_key, value):
    with pytest.raises(ValidationError):
        QAResult.model_validate(assessment(**{input_key: value}))


@pytest.mark.parametrize(
    ("severity", "standard_scope", "high_scope"),
    [
        (0, None, None),
        (0.1, None, None),
        (0.25, None, None),
        (0.35, None, "video"),
        (0.36, "video", "video"),
        (0.9, "video", "video"),
        (1, "video", "video"),
    ],
)
def test_severity_does_not_change_thresholds_or_invert_inconsistent_explanations(
    severity, standard_scope, high_scope
):
    result = QAResult.model_validate(assessment(artifact_severity=severity))
    assert result.artifact_score == severity
    assert result.retry_scope() == standard_scope
    assert result.retry_scope(high=True) == high_scope
    assert result.score() == pytest.approx((0.9 + 0.95 + 0.95 + 0.88 + 0.92 + 1 - severity) / 6)


def test_conflicting_new_and_legacy_fields_are_rejected():
    with pytest.raises(ValidationError):
        QAResult.model_validate(assessment(artifact_severity=0.1, artifact_score=0.9))


@pytest.mark.parametrize("input_key", ["artifact_severity", "artifact_score"])
async def test_visual_qa_transport_requests_severity_and_accepts_compatible_output(
    tmp_path, input_key
):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(assessment(**{input_key: 0.1}))}}]
            },
        )

    image = tmp_path / "current.png"
    Image.new("RGB", (16, 16), "yellow").save(image)
    provider = LLMProvider(
        Settings(_env_file=None, llm_base_url="http://fixture.invalid", vlm_model="fixture"),
        transport=httpx.MockTransport(handle),
    )
    result = await Directors(provider).qa(
        {},
        {"prompts": {"start_frame_prompt": "A yellow image."}},
        [image],
        "start_candidate",
    )
    assert len(requests) == 1
    system = requests[0]["messages"][0]["content"]
    assert '"artifact_severity"' in system
    assert '"artifact_score"' not in system
    assert "a clean image needs a value near 0" in system
    assert result.artifact_score == 0.1
    assert result.retry_scope() is None
