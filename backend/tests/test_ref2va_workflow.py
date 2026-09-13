"""Ref2VA reference selection must retain its guide, audio, and typed Autogrow slots."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.db.store import Store
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import input_definitions, patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.optional_references import select_optional_references
from app.workflows.schema import WorkflowImport


@pytest.fixture
def ref2va(tmp_path):
    folder = Path(__file__).resolve().parents[2] / "workflow_examples/h3_ref2va"
    body = WorkflowImport.model_validate_json((folder / "h3_ref2va.profile.json").read_text())
    profile = WorkflowManager(Store(tmp_path / "test.sqlite3")).import_workflow(body)
    metadata = {
        "MiniMaxH3ReferenceToVideo": {
            "input": {
                "optional": {
                    "ref_images": [
                        "COMFY_AUTOGROW_V3",
                        {
                            "template": {
                                "input": {"required": {"ref_image": ["IMAGE", {}]}},
                                "prefix": "ref_image_",
                                "min": 0,
                                "max": 9,
                            }
                        },
                    ]
                }
            }
        }
    }
    return profile, metadata


def test_ref2va_three_refs_keep_start_guide_and_native_audio(ref2va):
    profile, info = ref2va
    original = deepcopy(profile)
    assets = {
        "start_frame": "start.png",
        "reference_image": "owner.png",
        "reference_image_2": "room.png",
        "reference_image_3": "bowl.png",
    }
    selected = select_optional_references(profile, assets, info)
    assert not validate_bindings(selected)
    values, bindings, raw, _ = resolve_parameters(
        selected, {"duration": 5, "fps": 16, "prompt": "Scene"}, assets, {}, False, None
    )
    graph = patch(selected, {**values, **bindings}, raw)
    condition = next(n for n in graph.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert condition["inputs"]["length"] == 124
    for index, filename in enumerate(("owner.png", "room.png", "bowl.png")):
        link = condition["inputs"][f"ref_images.ref_image_{index}"]
        assert graph[link[0]]["inputs"]["image"] == filename
    assert "ref_images.ref_image_3" not in condition["inputs"]
    assert "REQUIRES_REAL_REFERENCE" not in json.dumps(graph)
    guide = next(n for n in graph.values() if n["class_type"] == "MiniMaxH3AddGuide")
    assert guide["inputs"]["frame_idx"] == 0
    assert graph[guide["inputs"]["image"][0]]["inputs"]["image"] == "start.png"
    video = next(n for n in graph.values() if n["class_type"] == "CreateVideo")
    assert video["inputs"]["fps"] == 24
    audio = graph[video["inputs"]["audio"][0]]
    images = graph[video["inputs"]["images"][0]]
    assert audio["inputs"]["samples"] == images["inputs"]["samples"]
    assert profile == original


@pytest.mark.parametrize("minimum,kind", [(2, "IMAGE"), (0, "AUDIO")])
def test_optional_autogrow_rejects_required_or_wrong_media_slot(ref2va, minimum, kind):
    profile, info = ref2va
    template = info["MiniMaxH3ReferenceToVideo"]["input"]["optional"]["ref_images"][1]["template"]
    template["min"] = minimum
    template["input"]["required"]["ref_image"][0] = kind
    with pytest.raises(AppError, match="可选 IMAGE"):
        select_optional_references(profile, {"reference_image": "owner.png"}, info)


def test_autogrow_does_not_infer_undeclared_or_out_of_range_slots(ref2va):
    profile, info = ref2va
    node = next(
        n for n in profile["workflow"].values() if n["class_type"] == "MiniMaxH3ReferenceToVideo"
    )
    unknown = (
        "ref_images.ref_image_9",
        "ref_images.ref_image_01",
        "ref_images.ref_image_x",
        "ref_image_0",
    )
    for field in unknown:
        node["inputs"][field] = ["101", 0]
    definitions = input_definitions(node, info)
    assert definitions["ref_images.ref_image_0"] == (["IMAGE", {}], False)
    assert not set(unknown).intersection(definitions)
