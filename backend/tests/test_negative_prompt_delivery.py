"""Instruction-only workflows must receive exclusions without rewriting user prompts."""

from copy import deepcopy

import pytest

from app.agents.audio import H3, audio_section
from app.core.errors import AppError
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import patch
from app.workflows.ownership import canonicalize
from tests.test_generation_preflight import uploaded_frame
from tests.test_native_audio_prompts import PROMPT
from tests.test_workflows import profile


def edit_profile(*, bound_negative=False):
    graph = {
        "0": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
        "5": {
            "class_type": "EditImage",
            "inputs": {"image": ["0", 0], "edit_instruction": "original instruction"},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["5", 0], "filename_prefix": "fixture"},
        },
    }
    bindings = {
        "prompt": {"node_id": "5", "input": "edit_instruction"},
        "reference_image": {"node_id": "0", "input": "image"},
    }
    if bound_negative:
        graph["exclude"] = {"class_type": "Text", "inputs": {"text": "original negative"}}
        bindings["negative"] = {"node_id": "exclude", "input": "text"}
    return canonicalize(
        {
            **profile(graph),
            "id": "instruction-image",
            "capability": "IMAGE_TO_IMAGE",
            "bindings": bindings,
            "outputs": {"image": "save"},
        }
    )


def test_i2i_exclusions_reach_actual_edit_instruction_without_mutating_inputs():
    workflow = edit_profile()
    original = deepcopy(workflow)
    automatic = {"prompt": "A yellow chick hatches in straw", "negative": "human hands, fire"}
    values, _, raw, sources = resolve_parameters(workflow, automatic, {}, {}, False)
    graph = patch(workflow, values, raw)
    instruction = graph["5"]["inputs"]["edit_instruction"]
    assert instruction.startswith(automatic["prompt"])
    assert "Visual exclusions" in instruction and instruction.endswith(automatic["negative"])
    assert "negative" not in workflow["bindings"]
    assert sources["5.edit_instruction"] == "ai"
    assert automatic["prompt"] == "A yellow chick hatches in straw"
    assert workflow == original


@pytest.mark.parametrize("negative", ["", "  \n\t", None])
def test_empty_exclusions_do_not_add_an_instruction_block(negative):
    workflow = edit_profile()
    values, *_ = resolve_parameters(
        workflow, {"prompt": "Unchanged target", "negative": negative}, {}, {}, False
    )
    assert values["prompt"] == "Unchanged target"


def test_explicit_user_prompt_remains_exact_even_with_automatic_exclusions():
    workflow = edit_profile()
    requested = "  Exact user instruction\n"
    values, _, raw, sources = resolve_parameters(
        workflow,
        {"prompt": "AI target", "negative": "human hands"},
        {},
        {"5.edit_instruction": requested},
        True,
        ai_values={"5.edit_instruction": "Different AI target"},
    )
    assert patch(workflow, values, raw)["5"]["inputs"]["edit_instruction"] == requested
    assert sources["5.edit_instruction"] == "user"


def test_exclusions_follow_the_resolved_dynamic_ai_prompt():
    workflow = edit_profile()
    values, _, raw, _ = resolve_parameters(
        workflow,
        {"prompt": "Old target", "negative": "human hands"},
        {},
        {},
        False,
        ai_values={"5.edit_instruction": "A chick hatching"},
    )
    instruction = patch(workflow, values, raw)["5"]["inputs"]["edit_instruction"]
    assert instruction.startswith("A chick hatching") and instruction.endswith("human hands")
    assert "Old target" not in instruction


def test_dedicated_negative_binding_keeps_both_resolved_user_overrides_exact():
    workflow = edit_profile(bound_negative=True)
    values, _, raw, sources = resolve_parameters(
        workflow,
        {"prompt": "AI target", "negative": "AI exclusions"},
        {},
        {"exclude.text": "User exclusions"},
        True,
        ai_values={"exclude.text": "Dynamic AI exclusions"},
    )
    graph = patch(workflow, values, raw)
    assert graph["5"]["inputs"]["edit_instruction"] == "AI target"
    assert graph["exclude"]["inputs"]["text"] == "User exclusions"
    assert sources["exclude.text"] == "user"


@pytest.mark.parametrize("bound_negative", [False, True])
def test_native_h3_does_not_turn_negative_catalog_into_positive_scene(system, bound_negative):
    workflow = deepcopy(system[1].state.store.get("workflow", "default_video"))
    workflow["capabilities"]["audio_prompt_format"] = H3
    if not bound_negative:
        workflow["bindings"].pop("negative", None)
    automatic = {"prompt": PROMPT, "negative": "extra people, deformed hands", "duration": 2}
    values, *_ = resolve_parameters(workflow, automatic, {}, {}, False)
    assert "extra people" not in values["prompt"]
    assert "Visual exclusions" not in values["prompt"]
    assert audio_section(values["prompt"]) == audio_section(PROMPT)
    assert values["negative"] == automatic["negative"]
    assert automatic["prompt"] == PROMPT


def test_combined_prompt_constraints_reject_overflow_without_truncation():
    workflow = edit_profile()
    with pytest.raises(AppError, match="提示词合并负向限制"):
        resolve_parameters(workflow, {"prompt": "x" * 19995, "negative": "hands"}, {}, {}, False)
    assert workflow["workflow"]["5"]["inputs"]["edit_instruction"] == "original instruction"


def test_combined_prompt_still_obeys_the_input_enum():
    workflow = edit_profile()
    item = next(p for p in workflow["parameters"] if p["key"] == "5.edit_instruction")
    item["enum"] = ["Allowed target"]
    with pytest.raises(AppError, match="提示词合并负向限制"):
        resolve_parameters(
            workflow, {"prompt": "Allowed target", "negative": "hands"}, {}, {}, False
        )


def test_instruction_only_engine_submission_and_completed_replay_are_idempotent(system):
    client, app, comfy = system
    workflow = edit_profile()
    comfy.info["EditImage"] = {
        "input": {"required": {"image": ["IMAGE", {}], "edit_instruction": ["STRING", {}]}},
        "output": ["IMAGE"],
    }
    asset_id = uploaded_frame(client, comfy)
    automatic = {"prompt": "A small chick in its nest", "negative": "human hands, fire"}
    arguments = (workflow, automatic, {"reference_image": asset_id}, "workflow-tests", None)
    job = app.state.engine.create_job(*arguments, "WORKFLOW_TEST", step_key="test:frame")
    rendered = client.portal.call(app.state.engine.run, job["id"])
    completed = app.state.store.get("job", job["id"])
    instruction = completed["patched_workflow"]["5"]["inputs"]["edit_instruction"]
    assert instruction == job["input_values"]["prompt"]
    assert instruction.count("Visual exclusions") == 1
    assert "human hands, fire" in instruction
    assert comfy.prompts[completed["comfy_prompt_id"]]["prompt"] == completed["patched_workflow"]
    reused = app.state.engine.create_job(*arguments, "WORKFLOW_TEST", step_key="test:frame")
    assert reused["id"] == job["id"]
    assert client.portal.call(app.state.engine.run, reused["id"]) == rendered
    assert len(comfy.prompts) == 1
