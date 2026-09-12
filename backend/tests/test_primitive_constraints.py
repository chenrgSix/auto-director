"""Primitive values must obey the receiving ComfyUI node's real constraints."""

from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.generation.parameters import fit_budget_dimensions, resolve_parameters
from app.workflows.analyzer import (
    analyze,
    check_value,
    patch,
    refresh_profile,
    validate_dependencies,
)
from app.workflows.dimensions import fit_dimensions
from app.workflows.ownership import decorate_parameters
from app.workflows.schema import Capabilities
from tests.test_api_pipeline import wait_episode
from tests.test_episode_rerun import complete


def primitive_video_profile(*, metadata=True):
    graph = {
        "115": {"class_type": "PrimitiveInt", "inputs": {"value": 384}},
        "147": {"class_type": "PrimitiveInt", "inputs": {"value": 672}},
        "prompt": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "a chick"}},
        "start": {"class_type": "LoadImage", "inputs": {"image": "start.png"}},
        "end": {"class_type": "LoadImage", "inputs": {"image": "end.png"}},
        "timing": {"class_type": "Timing", "inputs": {"duration": 3.0}},
        "condition": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "width": ["115", 0],
                "height": ["147", 0],
                "prompt": ["prompt", 0],
                "start_image": ["start", 0],
                "end_image": ["end", 0],
                "duration": ["timing", 0],
            },
        },
        "output": {"class_type": "SaveVideo", "inputs": {"video": ["condition", 0]}},
    }
    info = {
        "PrimitiveInt": {
            "input": {"required": {"value": ["INT", {"min": 0, "max": 100000, "step": 1}]}},
            "output": ["INT"],
        },
        "PrimitiveStringMultiline": {
            "input": {"required": {"value": ["STRING", {"multiline": True}]}},
            "output": ["STRING"],
        },
        "LoadImage": {
            "input": {"required": {"image": ["STRING", {"image_upload": True}]}},
            "output": ["IMAGE"],
        },
        "Timing": {
            "input": {"required": {"duration": ["FLOAT", {"min": 1, "max": 6}]}},
            "output": ["FLOAT"],
        },
        "MiniMaxH3ImageToVideo": {
            "input": {
                "required": {
                    "width": ["INT", {"min": 32, "max": 16384, "step": 32}],
                    "height": ["INT", {"min": 32, "max": 16384, "step": 32}],
                    "prompt": ["STRING", {}],
                    "start_image": ["IMAGE", {}],
                    "end_image": ["IMAGE", {}],
                    "duration": ["FLOAT", {"min": 1, "max": 6}],
                }
            },
            "output": ["VIDEO"],
        },
        "SaveVideo": {
            "input": {"required": {"video": ["VIDEO", {}]}},
            "output": [],
        },
    }
    result = {
        "id": "primitive-video",
        "name": "Primitive dimensions",
        "type": "video",
        "media_type": "video",
        "capability": "FIRST_LAST_TO_VIDEO",
        "capabilities": Capabilities(max_duration=6).model_dump(),
        "workflow": graph,
        **analyze(graph, info if metadata else None),
    }
    result["bindings"] = {
        role: {"node_id": node_id, "input": field}
        for role, node_id, field in [
            ("width", "115", "value"),
            ("height", "147", "value"),
            ("prompt", "prompt", "value"),
            ("start_frame", "start", "image"),
            ("end_frame", "end", "image"),
            ("duration", "timing", "duration"),
        ]
    }
    result["outputs"] = {"video": "output"}
    decorate_parameters(result)
    return result, info


def height_parameter(profile):
    return next(item for item in profile["parameters"] if item["key"] == "147.value")


def test_legacy_primitive_bindings_receive_live_constraints_before_budget_resolve_and_patch():
    legacy, info = primitive_video_profile(metadata=False)
    before, info_before = deepcopy(legacy), deepcopy(info)
    profile = refresh_profile(legacy, info)
    child = height_parameter(profile)["downstream_constraints"]
    assert len(child) == 1
    assert (child[0]["key"], child[0]["min"], child[0]["max"], child[0]["step"]) == (
        "condition.height",
        32,
        16384,
        32,
    )
    budget = {"width": 384, "height": 688, "max_duration": 6, "low_memory": True}
    fit_budget_dimensions({}, profile, budget)
    assert (budget["width"], budget["height"]) == (384, 672)
    values, _, raw, _ = resolve_parameters(
        profile, {**budget, "duration": 2.73}, {}, {}, False, budget
    )
    graph = patch(profile, values, raw)
    assert graph["115"]["inputs"]["value"] == 384
    assert graph["147"]["inputs"]["value"] == 672
    assert graph["condition"]["inputs"]["height"] == ["147", 0]
    assert legacy == before and info == info_before
    assert profile["workflow"] == legacy["workflow"]


@pytest.mark.parametrize("source", ["episode", "advanced", "patch"])
def test_invalid_explicit_primitive_dimensions_are_rejected_instead_of_silently_rounded(source):
    profile, _ = primitive_video_profile()
    budget = {"width": 384, "height": 688, "max_duration": 6}
    with pytest.raises(AppError, match="condition.height.*step"):
        if source == "episode":
            fit_budget_dimensions({"height": 688}, profile, budget)
        elif source == "advanced":
            resolve_parameters(profile, budget, {}, {"147.value": 688}, True, budget)
        else:
            patch(profile, {}, {"147.value": 688})


def test_valid_primitive_override_wins_and_oom_alignment_still_obeys_receiver():
    profile, _ = primitive_video_profile()
    budget = {"width": 384, "height": 688, "max_duration": 6, "low_memory": True}
    values, _, raw, sources = resolve_parameters(
        profile, budget, {}, {"147.value": 640}, True, budget
    )
    assert patch(profile, values, raw)["147"]["inputs"]["value"] == 640
    assert sources["147.value"] == "user"
    recovered, _, raw, sources = resolve_parameters(
        profile, {**budget, "height": 496}, {}, {"147.value": 640}, True, budget, recovery=True
    )
    assert patch(profile, recovered, raw)["147"]["inputs"]["value"] == 480
    assert sources["147.value"] == "system:oom"


def test_shared_primitive_satisfies_each_consumers_step_origin_and_bounds():
    profile, info = primitive_video_profile(metadata=False)
    profile["workflow"]["resize"] = {
        "class_type": "OtherResize",
        "inputs": {"height": ["147", 0]},
    }
    info["OtherResize"] = {
        "input": {"required": {"height": ["INT", {"min": 64, "max": 640, "step": 96}]}}
    }
    refreshed = refresh_profile(profile, info)
    assert {item["key"] for item in height_parameter(refreshed)["downstream_constraints"]} == {
        "condition.height",
        "resize.height",
    }
    assert fit_dimensions(refreshed, {"height": 688})["height"] == 640
    with pytest.raises(AppError, match="resize.height.*step"):
        check_value(height_parameter(refreshed), 608)


def test_incompatible_consumer_steps_fail_without_increasing_memory_budget():
    profile, info = primitive_video_profile(metadata=False)
    profile["workflow"]["resize"] = {"class_type": "OtherResize", "inputs": {"height": ["147", 0]}}
    info["OtherResize"] = {
        "input": {"required": {"height": ["INT", {"min": 16, "max": 2048, "step": 32}]}}
    }
    profile = refresh_profile(profile, info)
    with pytest.raises(AppError, match="没有合法尺寸"):
        fit_dimensions(profile, {"height": 688})


@pytest.mark.parametrize("source_class", ["ComfyMathExpression", "CustomInteger", "Reroute"])
def test_nonprimitive_nodes_do_not_inherit_constraints_by_guessing_transforms(source_class):
    profile, info = primitive_video_profile(metadata=False)
    profile["workflow"]["147"]["class_type"] = source_class
    info[source_class] = deepcopy(info["PrimitiveInt"])
    profile = refresh_profile(profile, info)
    assert not height_parameter(profile).get("downstream_constraints")
    assert fit_dimensions(profile, {"height": 688})["height"] == 688


def test_primitive_constraint_propagation_does_not_traverse_math_nodes_or_other_output_slots():
    profile, info = primitive_video_profile(metadata=False)
    profile["workflow"]["math"] = {
        "class_type": "Math",
        "inputs": {"value": ["147", 0], "expression": "value * 2"},
    }
    profile["workflow"]["condition"]["inputs"]["height"] = ["math", 0]
    info["Math"] = {
        "input": {"required": {"value": ["INT", {}], "expression": ["STRING", {}]}},
        "output": ["INT"],
    }
    refreshed = refresh_profile(profile, info)
    assert fit_dimensions(refreshed, {"height": 688})["height"] == 688
    assert all(
        item["key"] != "condition.height"
        for item in height_parameter(refreshed).get("downstream_constraints", [])
    )
    profile["workflow"]["condition"]["inputs"]["height"] = ["147", 1]
    refreshed = refresh_profile(profile, info)
    assert fit_dimensions(refreshed, {"height": 688})["height"] == 688
    assert any(
        issue.get("message") == "链接输出索引越界"
        for issue in validate_dependencies(refreshed, info)["issues"]
    )


@pytest.mark.parametrize("height,valid", [(672, True), (688, False), (16, False), (16416, False)])
def test_dependency_preflight_validates_primitive_literal_against_receiver(height, valid):
    profile, info = primitive_video_profile(metadata=False)
    profile["workflow"]["147"]["inputs"]["value"] = height
    before = deepcopy(profile)
    report = validate_dependencies(profile, info)
    assert report["valid"] is valid
    assert profile == before
    if not valid:
        assert any(issue["code"] == "WORKFLOW_INVALID" for issue in report["issues"])


@pytest.mark.parametrize(
    "node_class,node_type,source_value,target_spec,accepted,rejected",
    [
        (
            "PrimitiveFloat",
            "FLOAT",
            1.0,
            ["FLOAT", {"min": 0.25, "max": 1, "step": 0.25}],
            0.5,
            0.6,
        ),
        ("PrimitiveInt", "INT", 2, [[1, 2, 4], {}], 4, 3),
    ],
)
def test_receiver_type_enum_and_numeric_rules_are_kept_for_supported_primitives(
    node_class, node_type, source_value, target_spec, accepted, rejected
):
    graph = {
        "source": {"class_type": node_class, "inputs": {"value": source_value}},
        "sink": {"class_type": "Consumer", "inputs": {"setting": ["source", 0]}},
    }
    info = {
        node_class: {"input": {"required": {"value": [node_type, {}]}}, "output": [node_type]},
        "Consumer": {"input": {"required": {"setting": target_spec}}},
    }
    item = analyze(graph, info)["parameters"][0]
    check_value(item, accepted)
    with pytest.raises(AppError):
        check_value(item, rejected)


def test_failed_primitive_video_resumes_with_saved_story_and_frames(system):
    client, app, comfy = system
    receiving_classes = {}

    def use_primitive_dimensions(profile):
        graph = profile["workflow"]
        for role, default in [("width", 384), ("height", 672)]:
            binding = profile["bindings"][role]
            receiving_classes[role] = graph[binding["node_id"]]["class_type"]
            primitive_id = f"primitive-{role}"
            graph[primitive_id] = {"class_type": "PrimitiveInt", "inputs": {"value": default}}
            graph[binding["node_id"]]["inputs"][binding["input"]] = [primitive_id, 0]
            profile["bindings"][role] = {"node_id": primitive_id, "input": "value"}
        profile["parameters"] = analyze(graph)["parameters"]
        decorate_parameters(profile)

    app.state.store.update("workflow", "default_video", use_primitive_dimensions)
    comfy.info["PrimitiveInt"] = {
        "input": {"required": {"value": ["INT", {"min": 0, "max": 100000, "step": 1}]}},
        "output": ["INT"],
    }
    old = complete(system, width=None, height=None, memory_mode="low")
    assert old["budget"]["height"] == 688
    original_profile = deepcopy(app.state.store.get("workflow", "default_video"))
    original_job = next(
        job for job in app.state.store.list("job", old["id"]) if job["type"] == "SHOT_VIDEO"
    )
    assert original_job["patched_workflow"]["primitive-height"]["inputs"]["value"] == 688
    saved_frames = {
        key: client.get(f"/api/v1/assets/{old['shots'][0][key]}/file").content
        for key in ("start_frame_asset_id", "end_frame_asset_id")
    }
    for role, node_type in receiving_classes.items():
        comfy.info[node_type]["input"]["required"][role] = [
            "INT",
            {"min": 32, "max": 16384, "step": 32},
        ]

    def fail(episode):
        episode.update(status="FAILED", final_video_asset_id=None)
        episode["shots"][0].update(status="FAILED", video_asset_id=None)

    app.state.store.update("episode", old["id"], fail)
    submissions = len(comfy.prompts)
    response = client.post(f"/api/v1/episodes/{old['id']}/generate")
    assert response.status_code == 202, response.text
    final = wait_episode(client, old["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert (final["budget"]["width"], final["budget"]["height"]) == (384, 672)
    assert len(comfy.prompts) == submissions + 1
    for key in ("plan", "bible", "references"):
        assert final[key] == old[key]
    for key in ("start_frame_asset_id", "end_frame_asset_id", "prompts", "duration"):
        assert final["shots"][0][key] == old["shots"][0][key]
    for key, content in saved_frames.items():
        assert client.get(f"/api/v1/assets/{final['shots'][0][key]}/file").content == content
    assert final["metrics"]["llm_calls"] == old["metrics"]["llm_calls"]
    new_job = next(
        job for job in app.state.store.list("job", old["id"]) if job["type"] == "SHOT_VIDEO"
    )
    assert new_job["id"] != original_job["id"]
    assert new_job["patched_workflow"]["primitive-height"]["inputs"]["value"] == 672
    assert (
        app.state.store.get("job", original_job["id"])["patched_workflow"]
        == original_job["patched_workflow"]
    )
    assert (
        app.state.store.get("workflow", "default_video")["workflow"] == original_profile["workflow"]
    )
