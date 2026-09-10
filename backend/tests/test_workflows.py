from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.db.store import Store
from app.workflows.analyzer import analyze, patch, validate_dependencies, validate_graph
from app.workflows.manager import WorkflowManager
from app.workflows.schema import Capabilities


def image_graph():
    return {
        "text-x": {
            "class_type": "Text",
            "inputs": {"text": "original"},
            "_meta": {"title": "(Input:prompt)"},
        },
        "size-y": {
            "class_type": "Size",
            "inputs": {"width": 512, "height": 768},
            "_meta": {"title": "(Input:width_height)"},
        },
        "out-z": {
            "class_type": "Save",
            "inputs": {"image": ["text-x", 0]},
            "_meta": {"title": "(Output:image)"},
        },
    }


def profile(graph=None):
    graph = graph or image_graph()
    return {
        "workflow": graph,
        "type": "image",
        "capabilities": Capabilities().model_dump(),
        **analyze(graph),
    }


def test_roles_are_not_fixed_node_ids_and_patch_preserves_template():
    p = profile()
    before = deepcopy(p)
    result = patch(p, {"prompt": "new", "width": 640})
    assert result["text-x"]["inputs"]["text"] == "new"
    assert result["size-y"]["inputs"]["width"] == 640
    assert result["out-z"]["inputs"]["image"] == ["text-x", 0]
    assert p == before


def test_duplicate_and_ambiguous_roles_require_explicit_binding():
    graph = image_graph()
    graph["another"] = deepcopy(graph["text-x"])
    assert "prompt" not in analyze(graph)["bindings"]
    with pytest.raises(AppError):
        patch(profile(graph), {"prompt": "x"})


def test_ui_format_dangling_links_and_cycles_rejected():
    for graph in [
        {"nodes": []},
        {"a": {"class_type": "A", "inputs": {"x": ["b", 0]}}},
        {"a": {"class_type": "A", "inputs": {"x": ["a", 0]}}},
    ]:
        with pytest.raises(AppError):
            validate_graph(graph)


def test_missing_model_node_and_numeric_constraints_are_actionable():
    graph = image_graph()
    graph["model"] = {"class_type": "Loader", "inputs": {"ckpt_name": "missing.safetensors"}}
    info = {
        "Text": {"input": {"required": {"text": ["STRING", {}]}}, "output": ["IMAGE"]},
        "Size": {
            "input": {
                "required": {"width": ["INT", {"min": 64, "max": 1024}], "height": ["INT", {}]}
            }
        },
        "Loader": {"input": {"required": {"ckpt_name": [["installed.safetensors"], {}]}}},
    }
    p = profile(graph)
    checked = validate_dependencies(p, info)
    assert {issue["code"] for issue in checked["issues"]} == {"MISSING_NODE", "MISSING_MODEL"}
    p.update(analyze(graph, info))
    with pytest.raises(AppError):
        patch(p, {"width": 2048})
    with pytest.raises(AppError):
        patch(p, {"width": True})


def test_duration_is_converted_to_aligned_frames():
    p = profile()
    p["workflow"]["frames"] = {"class_type": "Frames", "inputs": {"length": 81}}
    p["bindings"]["duration"] = {
        "node_id": "frames",
        "input": "length",
        "transform": "duration_to_frames",
        "frame_multiple": 4,
        "frame_offset": 1,
    }
    assert patch(p, {"duration": 3, "fps": 16})["frames"]["inputs"]["length"] == 49


def test_cannot_override_link_or_unknown_parameter():
    p = profile()
    with pytest.raises(AppError):
        patch(p, {}, {"out-z.image": "danger"})
    p["bindings"]["prompt"] = {"node_id": "out-z", "input": "image"}
    with pytest.raises(AppError):
        patch(p, {"prompt": "danger"})


def test_bundled_profiles_are_runnable_structures_and_defaults_are_protected(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    manager.bootstrap()
    assert len(store.list("workflow")) == 2
    for p in store.list("workflow"):
        patch(p, {"prompt": "hello", "duration": 5, "fps": 16})
    with pytest.raises(AppError):
        manager.delete("default_image")
    store.close()


def test_numbered_references_and_three_duplicate_outputs():
    graph = image_graph()
    graph["ref"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "reference.png"},
        "_meta": {"title": "(Input:reference_image_1)"},
    }
    graph["out2"] = deepcopy(graph["out-z"])
    graph["out3"] = deepcopy(graph["out-z"])
    result = analyze(graph)
    assert result["bindings"]["reference_image_1"]["input"] == "image"
    assert "image" not in result["outputs"]
