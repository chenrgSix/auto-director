import re
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.agents.schemas import VisualContinuity
from app.core.errors import AppError
from app.generation.continuity import continuity_report, reference_roles
from app.generation.resolvers import ContinuityManager
from app.workflows.schema import WorkflowImport
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_image_graph
from tests.test_creation_packages import BASE, create, document, submit


def contract(character="officer", **changes):
    return {
        "scene_id": "study",
        "visible_character_ids": [character] if character else [],
        "reference_roles": [f"character:{character}"] if character else ["environment"],
        "framing": "medium",
        "state_in": {"paper.holder": "officer"},
        "state_out": {"paper.holder": "officer"},
        "intentional_jump": "",
        **changes,
    }


def episode():
    return {
        "bible": {"characters": [{"id": "writer"}, {"id": "officer"}]},
        "references": {
            "character:writer": "WRITER",
            "character:officer": "OFFICER",
            "environment": "ROOM",
            "style": "STYLE",
        },
        "shots": [
            {
                "id": "a",
                "index": 0,
                "enabled": True,
                "transition_from_previous": "CUT",
                "prompts": {"start_frame_prompt": "Only OFFICER", "visual_continuity": contract()},
            }
        ],
    }


PROFILE = {"bindings": {"reference_image": {}}}


def test_single_reference_follows_shot_identity_not_bible_order():
    e = episode()
    assert ContinuityManager.references(e, e["shots"][0], PROFILE) == {"reference_image": "OFFICER"}
    e["shots"][0]["prompts"]["visual_continuity"] = contract(None, framing="insert")
    assert ContinuityManager.references(e, e["shots"][0], PROFILE) == {"reference_image": "ROOM"}


def test_explicit_multi_reference_order_capacity_and_missing_assets():
    e = episode()
    s = e["shots"][0]
    s["prompts"]["visual_continuity"]["reference_roles"] += ["environment"]
    assert continuity_report(e, PROFILE)["warnings"][0]["code"] == "REFERENCE_CAPACITY"
    multi = {"bindings": {"reference_image_2": {}, "reference_image_1": {}}}
    assert ContinuityManager.references(e, s, multi) == {
        "reference_image_1": "OFFICER",
        "reference_image_2": "ROOM",
    }
    del e["references"]["character:officer"]
    with pytest.raises(AppError, match="参考素材"):
        ContinuityManager.references(e, s, PROFILE)


def test_legacy_ambiguous_reference_fails_without_guessing():
    e = episode()
    s = e["shots"][0]
    s["prompts"].pop("visual_continuity")
    with pytest.raises(AppError) as failure:
        reference_roles(e["bible"], s)
    assert failure.value.code == "SHOT_REFERENCE_REQUIRED"
    s["prompts"]["start_frame_prompt"] = "Only officer in view; no character catalog"
    assert reference_roles(e["bible"], s) == ["character:officer"]
    assert continuity_report(e, {"bindings": {}})["valid"]


def test_state_persists_across_reverse_shots_and_scene_return():
    e = episode()
    first = e["shots"][0]
    first["prompts"]["visual_continuity"]["state_out"] = {"paper.holder": "desk"}
    second = deepcopy(first)
    second.update(id="b", index=1)
    second["prompts"]["visual_continuity"] = contract(
        "writer", state_in={"writer.position": "left"}, state_out={"writer.position": "left"}
    )
    third = deepcopy(first)
    third.update(id="c", index=2)
    e["shots"] += [second, third]
    report = continuity_report(e, PROFILE)
    assert report["issues"][0]["shot_id"] == "c"
    assert report["issues"][0]["details"] == {
        "key": "paper.holder",
        "expected": "desk",
        "actual": "officer",
    }
    third["prompts"]["visual_continuity"]["intentional_jump"] = (
        "Later, the officer has picked the paper up."
    )
    assert continuity_report(e, PROFILE)["valid"]
    third["prompts"]["visual_continuity"]["intentional_jump"] = ""
    third["enabled"] = False
    assert continuity_report(e, PROFILE)["valid"]


@pytest.mark.parametrize(
    "change", [{"scene_id": "outside"}, {"framing": "close_up"}, {"intentional_jump": "Later"}]
)
def test_frame_continuation_cannot_also_request_a_camera_reset(change):
    e = episode()
    second = deepcopy(e["shots"][0])
    second.update(id="b", index=1, transition_from_previous="CONTINUE_FRAME")
    second["prompts"]["visual_continuity"].update(change)
    e["shots"].append(second)
    assert "CONTINUITY_FRAME_CONFLICT" in [
        i["code"] for i in continuity_report(e, PROFILE)["issues"]
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"reference_roles": ["character:writer"]},
        {"reference_roles": ["environment", "environment"]},
        {"state_in": {"": "value"}},
        {"visible_character_ids": ["officer", "officer"]},
    ],
)
def test_structural_contract_rejects_invalid_or_absent_subjects(change):
    with pytest.raises(ValidationError):
        VisualContinuity.model_validate(contract(**change))


def test_external_package_rejects_state_conflict_before_any_render(system):
    client, app, comfy = system
    package = document()
    for i, shot in enumerate(package["shots"]):
        shot["transition_from_previous"] = "CUT"
        shot["prompts"]["visual_continuity"] = contract(
            "lion",
            state_in={"lion.position": "left" if i == 0 else "wrong"},
            state_out={"lion.position": "left"},
        )
    project, _ = create(client, package)
    report = client.post(f"{BASE}/{project}/validate").json()
    assert not report["valid"] and report["issues"][0]["code"] == "CONTINUITY_STATE_CONFLICT"
    assert (
        not app.state.store.list("episode") and not app.state.store.list("job") and not comfy.calls
    )


def test_actual_patched_graph_uses_different_characters_and_state_constraints(system, monkeypatch):
    client, app, comfy = system
    original = comfy.handle

    def upload_names(request):
        if request.url.path == "/upload/image":
            filename = re.search(rb'filename="([^"]+)"', request.content)[1].decode()
            return httpx.Response(
                200, json={"name": filename, "subfolder": "autodirector", "type": "input"}
            )
        return original(request)

    monkeypatch.setattr(comfy, "handle", upload_names)
    comfy.info["VAEEncode"] = {"input": {"required": {"pixels": ["IMAGE", {}], "vae": ["VAE", {}]}}}
    store = app.state.store
    profile = app.state.workflows.import_workflow(
        WorkflowImport(
            name="single reference",
            capability="IMAGE_TO_IMAGE",
            workflow=image_to_image_graph(store.get("workflow", "default_image")["workflow"]),
        )
    )
    package = document()
    package["brief"]["image_workflow_id"] = profile["id"]
    package["bible"]["characters"].append(
        {"id": "officer", "description": "An officer", "distinguishing_features": ["gray cap"]}
    )
    for i, shot in enumerate(package["shots"]):
        shot["transition_from_previous"] = "CUT"
        shot["prompts"]["visual_continuity"] = contract("lion" if i == 0 else "officer")
    project, _ = create(client, package)
    delivered, _ = submit(client, project)
    eid = delivered["episode_id"]
    preview = client.get(f"/api/v1/episodes/{eid}").json()
    accepted = client.post(
        f"{BASE}/{project}/productions/{eid}/confirm",
        json={"request_id": str(uuid4()), "expected_version": preview["version"], "confirm": True},
    )
    assert accepted.status_code == 202, accepted.text
    final = wait_episode(client, eid)
    assert final["status"] == "COMPLETED", final.get("error")
    starts = sorted(
        (j for j in store.list("job", eid) if j["type"] == "SHOT_START_FRAME"),
        key=lambda j: j["created_at"],
    )
    assert [j["asset_bindings"]["reference_image"] for j in starts] == [
        final["references"]["character:lion"],
        final["references"]["character:officer"],
    ]
    assert (
        starts[0]["patched_workflow"]["reference"]["inputs"]["image"]
        != starts[1]["patched_workflow"]["reference"]["inputs"]["image"]
    )
    assert all("paper.holder" in j["input_values"]["prompt"] for j in starts)
    assert (
        final["shots"][1]["reference_selection"]["bindings"]["reference_image"]
        == final["references"]["character:officer"]
    )

    # Legacy episodes can rerun only their video without selecting character references again.
    def legacy(current):
        for shot in current["shots"]:
            shot["prompts"].pop("visual_continuity")
            shot["prompts"]["start_frame_prompt"] = "A person in the room, no stable character ID"

    store.update("episode", eid, legacy)
    before = store.get("episode", eid)
    response = client.post(
        f"/api/v1/episodes/{eid}/rerun",
        json={
            "expected_version": before["version"],
            "scope": "video",
            "shot_ids": [before["shots"][1]["id"]],
        },
    )
    assert response.status_code == 202, response.text
    rerun = wait_episode(client, eid)
    assert rerun["status"] == "COMPLETED", rerun.get("error")
    assert rerun["shots"][0]["video_asset_id"] == before["shots"][0]["video_asset_id"]
    assert rerun["shots"][1]["video_asset_id"] != before["shots"][1]["video_asset_id"]
    for shot, old in zip(rerun["shots"], before["shots"], strict=True):
        assert shot["start_frame_asset_id"] == old["start_frame_asset_id"]
        assert shot["end_frame_asset_id"] == old["end_frame_asset_id"]
    assert len([j for j in store.list("job", eid) if j["type"] == "SHOT_START_FRAME"]) == 2
