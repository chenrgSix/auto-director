import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.agents.directing import anchored_prompt
from app.agents.visual import visual_prose
from app.core.errors import AppError
from app.generation.continuity import continuity_report, reference_description, reference_roles
from app.generation.resolvers import ContinuityManager
from app.workflows.analyzer import analyze, patch, validate_bindings
from app.workflows.optional_references import select_optional_references
from app.workflows.ownership import canonicalize
from app.workflows.schema import WorkflowImport
from tests.test_shot_continuity import episode


def test_reference_compiler_uses_visual_values_only():
    style = {
        "image": "warm diffused lighting",
        "performance": "exaggerated crying",
        "sound": "music",
        "negative_prompt": "text, artifacts",
    }
    prompt = anchored_prompt({"style": style}, reference_description("style", style), {})
    assert "warm diffused lighting" in prompt
    for excluded in ('"image"', '"sound"', "music", "crying", "artifacts", "{"):
        assert excluded not in prompt
    assert visual_prose({"place": "kitchen", "声音": "滴答"}) == "kitchen"


def multi_profile():
    folder = Path(__file__).resolve().parents[2] / "workflow_examples/h3_multi_reference"
    request = WorkflowImport.model_validate_json(
        (folder / "h3_multi_reference.profile.json").read_text()
    )
    p = request.model_dump()
    p.update(analyze(request.workflow))
    p.update(
        bindings={r: b.model_dump() for r, b in request.bindings.items()},
        outputs=request.outputs,
        capability=request.capability.value,
    )
    p = canonicalize(p)
    assert not validate_bindings(p)
    info = {
        "H3ReferenceEditPrepare": {
            "input": {"optional": {f"reference_image_{i}": ["IMAGE", {}] for i in range(2, 10)}}
        }
    }
    return p, info


def test_shared_prop_order_and_optional_inputs_reach_actual_graph():
    e = episode()
    e["bible"]["props"] = [{"id": "bowl"}]
    e["references"]["prop:bowl"] = "BOWL"
    s = e["shots"][0]
    s["prompts"]["visual_continuity"]["reference_roles"] += ["environment", "prop:bowl"]
    p, info = multi_profile()
    assert continuity_report(e, p)["valid"]
    selected = ContinuityManager.references(e, s, p)
    assert list(selected.values()) == ["OFFICER", "ROOM", "BOWL"]
    reduced = select_optional_references(p, selected, info)
    graph = patch(reduced, {**selected, "prompt": "Only the requested scene"})
    assert graph["102"]["inputs"]["image"] == "ROOM"
    assert graph["103"]["inputs"]["image"] == "BOWL"
    assert "reference_image_4" not in graph["5"]["inputs"] and "104" not in graph
    assert "REQUIRES_REAL_REFERENCE" not in json.dumps(graph)
    assert "104" in p["workflow"]  # Stored profile is immutable.
    single = select_optional_references(p, {"reference_image": "ONLY"}, info)
    assert not any(r.startswith("reference_image_") for r in single["bindings"])
    del e["bible"]["props"]
    with pytest.raises(AppError, match="道具"):
        reference_roles(e["bible"], s)


def test_optional_reference_rejects_holes_and_required_consumer():
    p, info = multi_profile()
    with pytest.raises(AppError, match="连续选择"):
        select_optional_references(p, {"reference_image": "A", "reference_image_3": "C"}, info)
    info["H3ReferenceEditPrepare"]["input"]["optional"].pop("reference_image_2")
    with pytest.raises(AppError, match="可选 IMAGE"):
        select_optional_references(p, {"reference_image": "A"}, info)
    broken = deepcopy(p)
    broken["bindings"]["reference_image"]["optional"] = True
    assert validate_bindings(broken)


def test_engine_submits_only_selected_optional_images(system):
    from uuid import uuid4

    from tests.test_api_pipeline import wait_episode
    from tests.test_capabilities import image_to_image_graph
    from tests.test_creation_packages import BASE, create, document, submit
    from tests.test_shot_continuity import contract

    client, app, comfy = system
    graph = image_to_image_graph(app.state.store.get("workflow", "default_image")["workflow"])
    graph["extra"] = {"class_type": "LoadImage", "inputs": {"image": "UNUSED-PLACEHOLDER.png"}}
    graph["encode"]["inputs"]["other"] = ["extra", 0]
    comfy.info["VAEEncode"] = {
        "input": {
            "required": {"pixels": ["IMAGE", {}], "vae": ["VAE", {}]},
            "optional": {"other": ["IMAGE", {}]},
        }
    }
    bindings = analyze(graph)["bindings"]
    bindings["reference_image"] = {"node_id": "reference", "input": "image"}
    bindings["reference_image_2"] = {"node_id": "extra", "input": "image", "optional": True}
    p = app.state.workflows.import_workflow(
        WorkflowImport(
            name="Optional input fixture",
            capability="IMAGE_TO_IMAGE",
            workflow=graph,
            bindings=bindings,
            capabilities={"supports_multi_reference": True},
        )
    )
    package = document()
    package["brief"]["image_workflow_id"] = p["id"]
    for i, s in enumerate(package["shots"]):
        s["transition_from_previous"] = "CUT"
        s["prompts"]["visual_continuity"] = contract(
            "lion", reference_roles=["character:lion"] + (["environment"] if i else [])
        )
    project, _ = create(client, package)
    delivered, _ = submit(client, project)
    id = delivered["episode_id"]
    version = client.get(f"/api/v1/episodes/{id}").json()["version"]
    assert (
        client.post(
            f"{BASE}/{project}/productions/{id}/confirm",
            json={"request_id": str(uuid4()), "expected_version": version, "confirm": True},
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final.get("error")
    starts = sorted(
        [j for j in app.state.store.list("job", id) if j["type"] == "SHOT_START_FRAME"],
        key=lambda j: j["created_at"],
    )
    assert "extra" not in starts[0]["patched_workflow"]
    assert "other" not in starts[0]["patched_workflow"]["encode"]["inputs"]
    assert starts[1]["asset_bindings"]["reference_image_2"] == final["references"]["environment"]
    assert "UNUSED-PLACEHOLDER" not in json.dumps(starts[1]["patched_workflow"])
    assert "<Picture 2> supplies environment" in starts[1]["input_values"]["prompt"]


def test_reference_hints_follow_asset_overrides_and_keep_explicit_prompt_exact():
    from app.generation.continuity import ordered_reference_prompt

    profile, _ = multi_profile()
    assets = {"reference_image": "PERSON", "reference_image_2": "ROOM"}
    references = {"character:actor": "PERSON", "environment": "ROOM"}
    prompt = ordered_reference_prompt(
        profile, "Target", assets, references, {"reference_image": "UPLOAD"}
    )
    assert "<Picture 1> supplies the supplied visual reference" in prompt
    assert "character:actor" not in prompt
    assert "<Picture 2> supplies environment" in prompt
    assert (
        ordered_reference_prompt(profile, "Exact", assets, references, {"prompt": "Exact"})
        == "Exact"
    )
    end = ordered_reference_prompt(
        profile, "Endpoint", {**assets, "reference_image": "START"}, references, {}, "START"
    )
    assert "this shot's start state" in end and "approved" not in end
