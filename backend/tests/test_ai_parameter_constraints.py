import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts
from app.core.config import Settings
from app.generation.parameters import ai_parameters
from app.workflows.analyzer import analyze
from tests.fakes import FakeProvider


def linked_ai_profile():
    graph = {
        "source": {"class_type": "PrimitiveInt", "inputs": {"value": 16}},
        "consumer": {"class_type": "ShiftedInput", "inputs": {"value": ["source", 0]}},
        "choice": {"class_type": "SelectedInput", "inputs": {"value": ["source", 0]}},
    }
    info = {
        "PrimitiveInt": {
            "input": {"required": {"value": ["INT", {"min": 0, "max": 1024, "step": 1}]}}
        },
        "ShiftedInput": {
            "input": {"required": {"value": ["INT", {"min": 16, "max": 208, "step": 64}]}}
        },
        "SelectedInput": {"input": {"required": {"value": [[16, 144]]}}},
    }
    profile = {
        **analyze(graph, info),
        "id": "linked-ai",
        "media_type": "image",
        "capability": "TEXT_TO_IMAGE",
    }
    profile["parameters"][0]["owner"] = "ai"
    return profile


def mapped_schema(schema):
    return schema.model_json_schema()["properties"]["ai_parameters"]["properties"]["linked-ai"][
        "properties"
    ]["source.value"]


def test_live_downstream_constraints_reach_ai_context_and_schema_without_changing_profile():
    profile = linked_ai_profile()
    before = deepcopy(profile)
    specifications = ai_parameters(profile)
    assert specifications[0]["step"] == 1
    assert (
        specifications[0]["downstream_constraints"]
        == profile["parameters"][0]["downstream_constraints"]
    )
    field = mapped_schema(constrained_output(ShotPrompts, specifications))
    assert field["type"] == "integer"
    assert field["minimum"] == 0 and field["maximum"] == 1024 and field["multipleOf"] == 1
    shifted, choice = field["allOf"]
    assert shifted["type"] == "integer"
    assert shifted["minimum"] == 16 and shifted["maximum"] == 208
    assert "16 + n * 64" in shifted["description"]
    assert "consumer.value" in shifted["description"]
    assert "multipleOf" not in shifted  # 16 and 144 are valid although not multiples of 64.
    assert choice["enum"] == [16, 144] and choice["type"] == ["integer"]
    assert profile == before


@pytest.mark.parametrize("value", [16, 144])
async def test_local_ai_validation_accepts_values_satisfying_all_consumers(value):
    schema = constrained_output(ShotPrompts, ai_parameters(linked_ai_profile()))
    output = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    output["ai_parameters"] = {"linked-ai": {"source.value": value}}
    assert schema.model_validate(output).ai_parameters == output["ai_parameters"]


@pytest.mark.parametrize("value", [8, 64, 80, 208, 272, True, 16.0, "16", float("nan")])
async def test_local_ai_validation_rejects_downstream_type_bounds_step_and_enum(value):
    schema = constrained_output(ShotPrompts, ai_parameters(linked_ai_profile()))
    output = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    output["ai_parameters"] = {"linked-ai": {"source.value": value}}
    with pytest.raises(ValidationError):
        schema.model_validate(output)


@pytest.mark.parametrize(
    "minimum,step,valid,invalid", [(0, 0.25, 0.5, 0.3), (0.05, 0.1, 0.15, 0.1)]
)
async def test_float_step_origin_is_preserved_in_schema_and_strict_validation(
    minimum, step, valid, invalid
):
    specifications = [
        {
            "workflow_id": "linked-ai",
            "key": "source.value",
            "field": "value",
            "owner": "ai",
            "type": "number",
            "min": minimum,
            "max": 1,
            "step": step,
        }
    ]
    schema = constrained_output(ShotPrompts, specifications)
    field = mapped_schema(schema)
    assert f"{minimum} + n * {step}" in field["description"]
    assert field.get("multipleOf") == (step if minimum == 0 else None)
    output = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    output["ai_parameters"] = {"linked-ai": {"source.value": valid}}
    schema.model_validate(output)
    output["ai_parameters"]["linked-ai"]["source.value"] = invalid
    with pytest.raises(ValidationError):
        schema.model_validate(output)


async def test_provider_receives_all_constraints_and_repairs_illegal_downstream_value():
    schema = constrained_output(ShotPrompts, ai_parameters(linked_ai_profile()))
    output = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        # Both satisfy the step, but the first violates the other consumer's enum.
        output["ai_parameters"] = {"linked-ai": {"source.value": 80 if len(requests) == 1 else 144}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(respond)
    )
    result = await provider.generate_json("", {}, schema)
    assert result.ai_parameters == {"linked-ai": {"source.value": 144}}
    assert len(requests) == 2
    instruction = requests[0]["messages"][0]["content"]
    assert "16 + n * 64" in instruction and '"allOf"' in instruction
    assert '"enum": [16, 144]' in instruction
