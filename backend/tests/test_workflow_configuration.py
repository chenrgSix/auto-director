from copy import deepcopy

import pytest

from app.db.store import Store
from app.workflows.analyzer import validate_dependencies
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport, WorkflowPatch
from tests.test_workflows import image_graph, profile


@pytest.mark.parametrize(
    ("slots", "template", "valid"),
    [
        ({"values.a": ["size-y", 0]}, {"names": ["a", "b"], "min": 1}, True),
        ({}, {"names": ["a", "b"], "min": 1}, False),
        ({"values.a": None}, {"names": ["a", "b"], "min": 1}, False),
        ({"values.z": 5}, {"names": ["a", "b"], "min": 1}, False),
        ({"values.a.extra": 5}, {"names": ["a", "b"], "min": 1}, False),
        ({"values.a": 5}, {"names": ["a", "b"], "min": 2}, False),
        (
            {"values.ref_image_0": ["text-x", 0], "values.ref_image_1": ["text-x", 0]},
            {"prefix": "ref_image_", "min": 2},
            True,
        ),
        ({"values.ref_image_x": 5}, {"prefix": "ref_image_", "min": 1}, False),
        ({}, {"prefix": "ref_image_", "min": 0}, True),
    ],
)
def test_autogrow_required_container_uses_declared_slots(slots, template, valid):
    graph = image_graph()
    graph["math"] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "a * 24", **slots},
    }
    metadata = {
        "Text": {"output": ["IMAGE"]},
        "Size": {"output": ["FLOAT", "FLOAT"]},
        "Save": {},
        "ComfyMathExpression": {
            "input": {
                "required": {
                    "expression": ["STRING", {}],
                    "values": ["COMFY_AUTOGROW_V3", {"template": template}],
                }
            }
        },
    }
    candidate = profile(graph)
    before = deepcopy(candidate)
    result = validate_dependencies(candidate, metadata)
    assert result["valid"] is valid
    assert candidate == before
    if not valid:
        assert result["issues"] == [
            {
                "code": "WORKFLOW_INVALID",
                "node_id": "math",
                "field": "values",
                "message": "缺少节点必填字段",
            }
        ]


def test_autogrow_keeps_link_and_regular_required_field_validation():
    graph = image_graph()
    graph["math"] = {"class_type": "Math", "inputs": {"values.a": ["size-y", 7]}}
    info = {
        "Text": {},
        "Size": {"output": ["FLOAT"]},
        "Save": {},
        "Math": {
            "input": {
                "required": {
                    "expression": ["STRING", {}],
                    "values": ["COMFY_AUTOGROW_V3", {"template": {"names": ["a"], "min": 1}}],
                }
            }
        },
    }
    issues = validate_dependencies(profile(graph), info)["issues"]
    assert {(i["field"], i["message"]) for i in issues} == {
        ("expression", "缺少节点必填字段"),
        ("values.a", "链接输出索引越界"),
    }


@pytest.mark.parametrize(
    ("metadata", "mode", "api_ids"),
    [
        ({"Text": {}, "Size": {}, "Save": {}}, "local", []),
        ({"Text": {"api_node": True}, "Size": {}, "Save": {}}, "cloud", ["text-x"]),
        ({"Text": {"api_node": False}, "Size": {}, "Save": {}}, "local", []),
        ({"Text": {}, "Size": {}}, "unknown", []),
        ({"Text": {"api_node": True}}, "cloud", ["text-x"]),
    ],
)
def test_execution_mode_comes_from_metadata_not_model_name(tmp_path, metadata, mode, api_ids):
    manager = WorkflowManager(Store(tmp_path))
    imported = manager.import_workflow(
        WorkflowImport(
            name="MiniMax local/cloud cannot be inferred", type="image", workflow=image_graph()
        )
    )
    assert imported["execution_info"] == {"mode": "unknown", "api_nodes": []}
    checked = manager.validate(imported["id"], metadata)
    assert checked["execution_info"]["mode"] == mode
    assert [node["id"] for node in checked["execution_info"]["api_nodes"]] == api_ids
    # Editing values does not change which node classes this graph contains.
    saved = manager.update(imported["id"], WorkflowPatch(name="Renamed"))
    assert saved["execution_info"] == checked["execution_info"]
    assert manager.describe(manager.store.get("workflow", imported["id"])) == saved


def test_execution_info_is_returned_by_api_and_does_not_require_import_network(system):
    client, app, comfy = system
    graph = client.get("/api/v1/workflows/default_image").json()["workflow"]
    original_graph = deepcopy(graph)
    imported = client.post(
        "/api/v1/workflows/import",
        json={"name": "Configuration test", "type": "image", "workflow": graph},
    ).json()
    assert imported["execution_info"]["mode"] == "unknown"
    checked = client.post(f"/api/v1/workflows/{imported['id']}/validate").json()
    assert checked["execution_info"]["mode"] == "local"
    assert (
        client.get(f"/api/v1/workflows/{imported['id']}").json()["execution_info"]
        == checked["execution_info"]
    )
    listed = client.get("/api/v1/workflows").json()
    assert (
        next(p for p in listed if p["id"] == imported["id"])["execution_info"]
        == checked["execution_info"]
    )
    assert app.state.store.get("workflow", imported["id"])["workflow"] == original_graph
    assert not comfy.prompts
