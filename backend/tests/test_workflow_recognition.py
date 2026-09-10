import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.agents.provider import LLMProvider
from app.api.routes import analyze_workflow
from app.core.config import Settings
from app.core.errors import AppError
from app.db.store import Store
from app.workflows.analyzer import analyze, patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.recognition import recognize_workflow
from app.workflows.schema import WorkflowAnalyze, WorkflowImport
from tests.test_capabilities import image_to_image_graph, image_to_video_graph


def untag(graph):
    graph = deepcopy(graph)
    for node in graph.values():
        node.pop("_meta", None)
    return graph


def proposal(capability, bindings, outputs):
    return {
        "capability": capability,
        "bindings": {
            role: {**binding, "reason": "模型提供的用途说明"} for role, binding in bindings.items()
        },
        "outputs": {
            media: {"node_id": node, "reason": "最终输出"} for media, node in outputs.items()
        },
        "notes": [],
    }


def mock_provider(result, calls):
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    return LLMProvider(Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(respond))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability", ["TEXT_TO_IMAGE", "IMAGE_TO_IMAGE", "FIRST_LAST_TO_VIDEO", "IMAGE_TO_VIDEO"]
)
async def test_ai_proposal_can_be_applied_to_real_patch_without_rule_inference(
    tmp_path, capability
):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    media = "image" if capability.endswith("TO_IMAGE") else "video"
    graph = store.get("workflow", f"default_{media}")["workflow"]
    if capability == "IMAGE_TO_IMAGE":
        graph = image_to_image_graph(graph)
    if capability == "IMAGE_TO_VIDEO":
        graph = image_to_video_graph(graph)
    expected = analyze(graph)
    graph = untag(graph)
    # An arbitrary custom class/field needs no registration in node-name heuristics.
    graph["positive"]["class_type"] = "StudioCustomEncoder"
    graph["positive"]["inputs"]["creative_brief"] = graph["positive"]["inputs"].pop("text")
    expected["bindings"]["prompt"]["input"] = "creative_brief"
    before = deepcopy(graph)
    local = analyze(graph)
    assert local["bindings"] == {} and local["outputs"] == {}
    calls = []
    response = await recognize_workflow(
        graph, mock_provider(proposal(capability, expected["bindings"], expected["outputs"]), calls)
    )
    assert len(calls) == 1
    assert "StudioCustomEncoder" in calls[0]["messages"][1]["content"]
    profile = manager.import_workflow(
        WorkflowImport(
            name="AI approved",
            workflow=graph,
            **{k: response[k] for k in ("capability", "bindings", "outputs")},
        )
    )
    assert not validate_bindings(profile)
    patched = patch(profile, {"prompt": "actual generation", "duration": 2, "fps": 16})
    assert patched["positive"]["inputs"]["creative_brief"] == "actual generation"
    assert patched["sampler"]["inputs"]["positive"] == graph["sampler"]["inputs"]["positive"]
    assert graph == before
    store.close()


def test_import_and_read_do_not_contact_comfy_or_ai_or_repeat_analysis(system, monkeypatch):
    client, app, comfy = system
    graph = untag(app.state.store.get("workflow", "default_image")["workflow"])
    before = list(comfy.calls)

    def forbidden(*args, **kwargs):
        raise AssertionError("Import/read must not contact external services or re-analyze")

    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    monkeypatch.setattr(app.state.engine, "client", forbidden)
    response = client.post(
        "/api/v1/workflows/import",
        json={"name": "Manual", "capability": "TEXT_TO_IMAGE", "workflow": graph},
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["bindings"] == {} and profile["outputs"] == {} and profile["validation"] is None
    assert profile["binding_issues"]
    monkeypatch.setattr("app.workflows.manager.analyze", forbidden)
    assert client.get("/api/v1/workflows").status_code == 200
    assert client.get(f"/api/v1/workflows/{profile['id']}").status_code == 200
    edited = client.patch(
        f"/api/v1/workflows/{profile['id']}",
        json={
            "bindings": {"prompt": {"node_id": "positive", "input": "text"}},
            "outputs": {"image": "save"},
        },
    )
    assert edited.status_code == 200
    assert client.post(f"/api/v1/workflows/{profile['id']}/auto-bind").status_code == 410
    assert comfy.calls == before


def test_ai_endpoint_is_read_only_and_does_not_fetch_object_info(system, monkeypatch):
    client, app, comfy = system
    original = app.state.store.list("workflow")
    profile = original[0]
    calls = []
    monkeypatch.setattr(
        app.state.generation,
        "provider_factory",
        lambda: mock_provider(
            proposal(profile["capability"], profile["bindings"], profile["outputs"]), calls
        ),
    )
    response = client.post("/api/v1/workflows/analyze", json={"workflow": profile["workflow"]})
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and not comfy.calls
    assert app.state.store.list("workflow") == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "node",
        "field",
        "link",
        "duplicate",
        "numeric_type",
        "text_type",
        "role",
        "output",
        "output_media",
        "transform",
        "frame_rule",
        "extra",
        "capability",
    ],
)
async def test_illegal_ai_bindings_are_rejected_without_fallback(invalid):
    graph = {
        "text": {"class_type": "Custom", "inputs": {"words": "abc", "number": 81}},
        "save": {"class_type": "CustomSave", "inputs": {"images": ["text", 0]}},
    }
    value = proposal(
        "TEXT_TO_IMAGE", {"prompt": {"node_id": "text", "input": "words"}}, {"image": "save"}
    )
    binding = value["bindings"]["prompt"]
    if invalid == "node":
        binding["node_id"] = "absent"
    if invalid == "field":
        binding["input"] = "absent"
    if invalid == "link":
        binding.update(node_id="save", input="images")
    if invalid == "duplicate":
        value["bindings"]["negative"] = deepcopy(binding)
    if invalid == "numeric_type":
        value["bindings"] = {"width": binding}
    if invalid == "text_type":
        binding["input"] = "number"
    if invalid == "role":
        value["bindings"] = {"execute_shell": binding}
    if invalid == "output":
        value["outputs"]["image"]["node_id"] = "absent"
    if invalid == "output_media":
        value["outputs"] = {"video": value["outputs"]["image"]}
    if invalid == "transform":
        binding["transform"] = "duration_to_frames"
    if invalid == "frame_rule":
        value = proposal(
            "IMAGE_TO_VIDEO",
            {"duration": {"node_id": "text", "input": "number", "transform": "duration_to_frames"}},
            {"video": "save"},
        )
    if invalid == "extra":
        value["workflow"] = graph
    if invalid == "capability":
        value["capability"] = "IMAGE_TO_VIDEO"
    calls = []
    with pytest.raises(AppError) as exc:
        await recognize_workflow(
            graph, mock_provider(value, calls), "TEXT_TO_IMAGE" if invalid == "capability" else None
        )
    assert exc.value.code == "LLM_INVALID_OUTPUT"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_timeout_cancels_model_request_and_partial_results_are_allowed(monkeypatch):
    graph = {"text": {"class_type": "Custom", "inputs": {"words": "abc"}}}
    cancelled = asyncio.Event()

    class SlowProvider:
        async def generate_json(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr("app.workflows.recognition.AI_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AppError) as exc:
        await recognize_workflow(graph, SlowProvider())
    assert exc.value.code == "WORKFLOW_AI_TIMEOUT" and cancelled.is_set()
    partial = proposal("TEXT_TO_IMAGE", {}, {})
    partial["notes"] = ["无法确定输出，请手动确认"]
    result = await recognize_workflow(graph, mock_provider(partial, []))
    assert result["bindings"] == {} and result["notes"]


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_recognition():
    started, cancelled = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        async def generate_json(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    request = SimpleNamespace(
        receive=receive,
        app=SimpleNamespace(
            state=SimpleNamespace(generation=SimpleNamespace(provider_factory=SlowProvider))
        ),
    )
    with pytest.raises(AppError) as exc:
        await analyze_workflow(
            request,
            WorkflowAnalyze(workflow={"a": {"class_type": "Custom", "inputs": {"text": "x"}}}),
        )
    assert exc.value.code == "REQUEST_CANCELLED" and cancelled.is_set()


def test_no_model_and_missing_purpose_do_not_prevent_manual_import(system, monkeypatch):
    client, app, comfy = system
    graph = untag(app.state.store.get("workflow", "default_image")["workflow"])
    monkeypatch.setattr(
        app.state.generation,
        "provider_factory",
        lambda: LLMProvider(Settings(_env_file=None, llm_model="")),
    )
    assert client.post("/api/v1/workflows/analyze", json={"workflow": graph}).status_code == 409
    assert (
        client.post(
            "/api/v1/workflows/import", json={"name": "Choose purpose", "workflow": graph}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/workflows/import",
            json={"name": "Manual", "capability": "TEXT_TO_IMAGE", "workflow": graph},
        ).status_code
        == 201
    )
    assert not comfy.calls


@pytest.mark.asyncio
async def test_ai_context_redacts_credentials_and_rejects_oversized_graph():
    graph = {
        "custom": {"class_type": "Custom", "inputs": {"api_key": "private-key", "prompt": "words"}}
    }
    calls = []
    await recognize_workflow(graph, mock_provider(proposal("TEXT_TO_IMAGE", {}, {}), calls))
    assert "private-key" not in calls[0]["messages"][1]["content"]
    graph["custom"]["inputs"].update({f"text_{i}": "x" * 512 for i in range(200)})
    with pytest.raises(AppError) as exc:
        await recognize_workflow(graph, mock_provider({}, calls))
    assert exc.value.code == "WORKFLOW_TOO_LARGE_FOR_AI" and len(calls) == 1
