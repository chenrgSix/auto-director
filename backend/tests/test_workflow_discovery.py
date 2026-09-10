from copy import deepcopy

import pytest

from app.db.store import Store
from app.workflows.analyzer import analyze, patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport, WorkflowPatch
from tests.test_capabilities import image_to_image_graph


def untag(graph):
    graph = deepcopy(graph)
    for node in graph.values():
        node.pop("_meta", None)
    return graph


@pytest.mark.parametrize(
    "capability", ["TEXT_TO_IMAGE", "IMAGE_TO_IMAGE", "FIRST_LAST_TO_VIDEO", "IMAGE_TO_VIDEO"]
)
def test_untagged_common_graphs_infer_capability_and_writable_bindings(tmp_path, capability):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    media = "image" if capability.endswith("TO_IMAGE") else "video"
    graph = untag(store.get("workflow", f"default_{media}")["workflow"])
    if capability == "IMAGE_TO_IMAGE":
        graph = untag(image_to_image_graph(graph))
    if capability == "IMAGE_TO_VIDEO":
        graph["frames"]["class_type"] = "WanImageToVideo"
        del graph["end"]
        del graph["end_vision"]
        graph["frames"]["inputs"].pop("end_image")
        graph["frames"]["inputs"].pop("clip_vision_end_image")
    before = deepcopy(graph)
    profile = manager.import_workflow(WorkflowImport(name="No tags", workflow=graph))
    assert profile["capability"] == capability
    assert not validate_bindings(profile)
    assert profile["bindings"]["prompt"]["node_id"] == "positive"
    assert profile["bindings"]["negative"]["node_id"] == "negative"
    if media == "video":
        assert ("end_frame" in profile["bindings"]) == (capability == "FIRST_LAST_TO_VIDEO")
        assert profile["bindings"]["duration"]["transform"] == "duration_to_frames"
    patched = patch(profile, {"prompt": "new", "duration": 2, "fps": 16})
    assert patched["positive"]["inputs"]["text"] == "new"
    assert patched["sampler"]["inputs"]["positive"] == graph["sampler"]["inputs"]["positive"]
    assert graph == before
    store.close()


def test_multiple_prompts_and_outputs_require_choice_and_exclude_preview(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    graph = untag(store.get("workflow", "default_image")["workflow"])
    graph["second_text"] = deepcopy(graph["positive"])
    graph["second_sampler"] = deepcopy(graph["sampler"])
    graph["second_sampler"]["inputs"]["positive"] = ["second_text", 0]
    graph["second_save"] = deepcopy(graph["save"])
    graph["preview"] = {"class_type": "PreviewImage", "inputs": {"images": ["decode", 0]}}
    result = analyze(graph)
    assert "prompt" not in result["bindings"] and "image" not in result["outputs"]
    assert {c["key"] for c in result["binding_assistance"]["inputs"]["prompt"]} == {
        "positive.text",
        "second_text.text",
    }
    assert {c["node_id"] for c in result["binding_assistance"]["outputs"]["image"]} == {
        "save",
        "second_save",
    }
    store.close()


def test_unknown_frame_alignment_and_wrong_parameter_type_are_not_guessed(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    graph = untag(store.get("workflow", "default_video")["workflow"])
    graph["frames"]["class_type"] = "CustomVideo"
    graph["frames"]["inputs"]["width"] = "not a dimension"
    result = analyze(graph)
    assert "duration" not in result["bindings"] and "width" not in result["bindings"]
    candidate = result["binding_assistance"]["inputs"]["duration"][0]
    assert not candidate["automatic"] and "确认" in candidate["reason"]
    store.close()


def test_auto_bind_preserves_manual_fields_and_existing_conversion_rules(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    graph = untag(store.get("workflow", "default_video")["workflow"])
    profile = manager.import_workflow(WorkflowImport(name="Manual", workflow=graph))
    custom = {
        "prompt": profile["bindings"]["negative"],
        "duration": {**profile["bindings"]["duration"], "frame_multiple": 8},
    }
    manager.update(profile["id"], WorkflowPatch(bindings=custom))
    restored = manager.auto_bind(profile["id"])
    assert restored["bindings"]["prompt"] == custom["prompt"]
    assert restored["bindings"]["duration"]["frame_multiple"] == 8
    assert "negative" not in restored["bindings"]
    assert restored["bindings"]["start_frame"]
    store.close()


def test_analysis_preview_is_read_only_and_binding_status_separate_from_models(system):
    client, app, comfy = system
    original = app.state.store.list("workflow")
    graph = untag(client.get("/api/v1/workflows/default_image").json()["workflow"])
    response = client.post("/api/v1/workflows/analyze", json={"workflow": graph})
    assert (
        response.status_code == 200
        and response.json()["binding_assistance"]["suggested_capability"] == "TEXT_TO_IMAGE"
    )
    assert app.state.store.list("workflow") == original
    response = client.post("/api/v1/workflows/import", json={"name": "Auto", "workflow": graph})
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["binding_issues"] == [] and profile["validation"] is None
    comfy.info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [["other.safetensors"]]
    result = client.post(f"/api/v1/workflows/{profile['id']}/validate").json()
    assert result["binding_issues"] == []
    assert not result["validation"]["valid"]
    assert {issue["code"] for issue in result["validation"]["issues"]} == {"MISSING_MODEL"}
    result = client.post(f"/api/v1/workflows/{profile['id']}/auto-bind")
    assert result.status_code == 200
