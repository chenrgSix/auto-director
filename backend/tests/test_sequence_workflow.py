import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.db.store import Store
from app.workflows.analyzer import patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport
from app.workflows.sequence import compile_sequence, exported_lengths, sequence_input

FOLDER = Path(__file__).resolve().parents[2] / "workflow_examples/codex_h3_continuous"


@pytest.fixture
def sequence_profile(tmp_path):
    request = WorkflowImport.model_validate_json((FOLDER / "continuous.profile.json").read_text())
    return WorkflowManager(Store(tmp_path / "sequence.sqlite3")).import_workflow(request)


def sequence_values():
    return {
        "width": 768,
        "height": 448,
        "fps": 24,
        "seed": 42,
        "duration": 5,
        "_sequence": {
            "segments": [
                {
                    "shot_id": "first",
                    "prompt": "Owner pushes bowl",
                    "duration": 5,
                    "references": ["reference_image", "reference_image_2"],
                },
                {
                    "shot_id": "second",
                    "prompt": "Owner turns toward window",
                    "duration": 5,
                    "references": ["reference_image_2", "reference_image"],
                },
            ]
        },
    }


def test_canvas_adapter_has_no_story_assets_or_keyframes(sequence_profile):
    profile = sequence_profile
    assert not validate_bindings(profile)
    assert not profile["capabilities"]["supports_start_frame"]
    assert not profile["capabilities"]["supports_end_frame"]
    assert profile["workflow"] == json.loads((FOLDER / "continuous.api.json").read_text())
    assert "eb8c7268" not in json.dumps(profile) and "Picture 4" not in json.dumps(profile)


def test_compile_preserves_order_paths_and_exact_frame_clock(sequence_profile):
    values = sequence_values()
    before = deepcopy(sequence_profile)
    graph = compile_sequence(
        sequence_profile,
        patch(sequence_profile, values),
        values,
        {"reference_image": "autodirector/owner.png", "reference_image_2": "autodirector/bowl.png"},
    )
    node = graph[sequence_profile["bindings"]["sequence"]["node_id"]]["inputs"]
    timeline = json.loads(node["timeline_data"])
    assert node["total_frames"] == 248
    assert timeline["segments"][1]["refs"][0]["imageFile"] == "autodirector/bowl.png"
    assert [s["continuityFromPrev"] for s in timeline["segments"]] == [False, True]
    assert not timeline["runSelectEnabled"] and not timeline["global"]["refs"]
    assert timeline["output"]["exportMode"] == "all"
    assert sequence_profile == before


@pytest.mark.parametrize("path", ["../owner.png", "/input/owner.png", "C:\\owner.png", ""])
def test_missing_or_unsafe_uploaded_reference_is_blocked(sequence_profile, path):
    values = sequence_values()
    with pytest.raises(AppError):
        compile_sequence(
            sequence_profile,
            patch(sequence_profile, values),
            values,
            {"reference_image": path, "reference_image_2": "bowl.png"},
        )


def test_invalid_sequence_ids_capacity_and_empty_references_are_rejected():
    for kind in ("duplicate", "many", "empty"):
        values = sequence_values()
        segments = values["_sequence"]["segments"]
        if kind == "duplicate":
            segments[1]["shot_id"] = segments[0]["shot_id"]
        elif kind == "many":
            values["_sequence"]["segments"] = segments * 5
        else:
            segments[0]["references"] = []
        with pytest.raises(AppError, match="连续镜头组"):
            sequence_input(values)


def test_report_requires_actual_references_motion_and_full_frame_count(sequence_profile):
    report = (
        "Segment 1/2: r2v — Reference-to AV (~2 ref image(s), 0 ref video(s))\n"
        "Seg #2: continuity guide — 22f from seg #1 (AV latent, +audio); sample=158f → export 136f\n"
        "Segment 2/2: r2v — Reference-to AV (~2 ref image(s), 0 ref video(s))"
    )
    output = sequence_profile["outputs"]["sequence_report"]
    history = {"outputs": {output: {"text": [report]}}}
    assert exported_lengths(sequence_profile, sequence_values(), history, 260)[0] == [124, 136]
    for bad, frames in (
        (report.replace("~2 ref", "~0 ref"), 260),
        (report, 248),
        (report.replace("AV latent, +audio", "image only"), 260),
        (report + "\nPartial run: 1", 260),
    ):
        with pytest.raises(AppError):
            exported_lengths(
                sequence_profile, sequence_values(), {"outputs": {output: {"text": [bad]}}}, frames
            )


def test_adapter_rejects_disconnected_report_and_wrong_output(sequence_profile):
    for output in ("sequence_report", "video"):
        profile = deepcopy(sequence_profile)
        profile["outputs"][output] = "missing"
        assert validate_bindings(profile)
