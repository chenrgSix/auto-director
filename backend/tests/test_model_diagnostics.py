import asyncio
import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.agents.diagnostics import ModelTestRequest
from app.agents.provider import LLMProvider
from app.api.routes import test_model as model_test_route
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app


@pytest.fixture
def model_system(tmp_path):
    config = Settings(
        _env_file=None,
        data_dir=tmp_path,
        llm_model="director-fixture",
        vlm_model="vision-fixture",
        llm_base_url="https://models.example/v1",
        llm_api_key="private-test-key",
    )
    app = create_app(config)
    with TestClient(app) as client:
        yield client, app


def wire(app, monkeypatch, handler):
    provider = LLMProvider(app.state.config, httpx.MockTransport(handler))
    monkeypatch.setattr(app.state.generation, "provider_factory", lambda: provider)
    return provider


def completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.parametrize("kind", ["director", "vision"])
def test_model_probe_uses_saved_protocol_model_and_key_without_creating_records(
    model_system, monkeypatch, kind
):
    client, app = model_system
    calls = []

    def respond(request):
        calls.append(request)
        return completion('{"color":"blue"}' if kind == "vision" else '{"result":"ok"}')

    wire(app, monkeypatch, respond)
    before = {
        key: app.state.store.list(key)
        for key in ("settings", "workflow", "episode", "job", "asset")
    }
    result = client.post("/api/v1/models/test", json={"kind": kind})
    assert result.status_code == 200 and result.json()["success"], result.text
    assert result.json()["model"] == f"{kind}-fixture"
    assert result.json()["elapsed_seconds"] >= 0
    assert result.json()["timeout_seconds"] == 120
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://models.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer private-test-key"
    payload = json.loads(request.content)
    assert payload["model"] == f"{kind}-fixture"
    assert payload["response_format"] == {"type": "json_object"}
    if kind == "vision":
        content = payload["messages"][1]["content"]
        assert content[1]["type"] == "image_url"
        data_url = content[1]["image_url"]["url"]
        with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as image:
            assert image.size == (32, 32)
            r, g, b = image.getpixel((0, 0))
            assert b > 250 and r < 5 and g < 5
        assert result.json()["checks"] == ["json_output", "image_input"]
    else:
        assert isinstance(payload["messages"][1]["content"], str)
        assert result.json()["checks"] == ["json_output"]
    assert "private-test-key" not in result.text
    assert {key: app.state.store.list(key) for key in before} == before


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "LLM_AUTH_FAILED"),
        (403, "LLM_ACCESS_DENIED"),
        (404, "LLM_NOT_FOUND"),
        (429, "LLM_RATE_LIMITED"),
        (400, "LLM_REQUEST_REJECTED"),
        (422, "LLM_REQUEST_REJECTED"),
        (503, "LLM_UPSTREAM_ERROR"),
    ],
)
def test_model_http_failures_are_specific_and_never_echo_upstream_secrets(
    model_system, monkeypatch, status, code
):
    client, app = model_system
    wire(
        app,
        monkeypatch,
        lambda _: httpx.Response(
            status, json={"error": "private-test-key sensitive upstream details"}
        ),
    )
    response = client.post("/api/v1/models/test", json={"kind": "director"})
    data = response.json()
    assert response.status_code == 200 and not data["success"]
    assert data["checks"] == [] and data["error"]["code"] == code
    assert data["error"]["details"]["http_status"] == status
    assert "private-test-key" not in response.text and "sensitive upstream" not in response.text


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout, "LLM_TIMEOUT"),
        (httpx.ConnectTimeout, "LLM_TIMEOUT"),
        (httpx.WriteTimeout, "LLM_TIMEOUT"),
        (httpx.PoolTimeout, "LLM_TIMEOUT"),
        (httpx.ConnectError, "LLM_CONNECT_ERROR"),
        (httpx.RemoteProtocolError, "LLM_NETWORK_ERROR"),
    ],
)
def test_network_failures_preserve_safe_exception_category(
    model_system, monkeypatch, exception, code
):
    client, app = model_system

    def fail(request):
        raise exception("private-test-key sensitive exception details", request=request)

    wire(app, monkeypatch, fail)
    response = client.post("/api/v1/models/test", json={"kind": "director"})
    data = response.json()
    assert not data["success"] and data["error"]["code"] == code
    assert data["error"]["details"]["exception_type"] == exception.__name__
    assert "private-test-key" not in response.text and "sensitive exception" not in response.text


@pytest.mark.parametrize(
    ("kind", "raw", "code", "attempts"),
    [
        ("director", "not JSON", "LLM_INVALID_OUTPUT", 2),
        ("director", '{"result":"wrong"}', "LLM_INVALID_OUTPUT", 2),
        ("vision", '{"color":"red"}', "LLM_VISION_TEST_FAILED", 1),
    ],
)
def test_invalid_json_or_wrong_image_answer_is_not_reported_as_success(
    model_system, monkeypatch, kind, raw, code, attempts
):
    client, app = model_system
    provider = wire(app, monkeypatch, lambda _: completion(raw))
    data = client.post("/api/v1/models/test", json={"kind": kind}).json()
    assert not data["success"] and data["error"]["code"] == code
    assert provider.usage["calls"] == attempts


def test_missing_model_and_invalid_test_inputs_make_no_model_request(model_system, monkeypatch):
    client, app = model_system
    app.state.config.vlm_model = ""

    def forbidden(_):
        raise AssertionError("No model request expected")

    wire(app, monkeypatch, forbidden)
    data = client.post("/api/v1/models/test", json={"kind": "vision"}).json()
    assert not data["success"] and data["error"]["code"] == "CONFIGURATION_REQUIRED"
    for payload in (
        {"kind": "video"},
        {"kind": "director", "llm_api_key": "injected"},
        {"kind": "director", "endpoint": "https://untrusted.example"},
    ):
        assert client.post("/api/v1/models/test", json=payload).status_code == 422


def test_malformed_response_envelopes_return_json_diagnostic_instead_of_internal_error(
    model_system, monkeypatch
):
    client, app = model_system
    for payload in ([], None, {"choices": [{"message": []}]}):
        wire(
            app,
            monkeypatch,
            lambda _, payload=payload: httpx.Response(
                200, content=json.dumps(payload), headers={"content-type": "application/json"}
            ),
        )
        response = client.post("/api/v1/models/test", json={"kind": "director"})
        assert response.status_code == 200, response.text
        assert not response.json()["success"]
        assert response.json()["error"]["code"] == "LLM_INVALID_OUTPUT"


def test_probe_deadline_cancels_request_and_cleans_up_test_image(model_system, monkeypatch):
    client, app = model_system
    paths = []
    cancelled = []

    class Slow:
        async def generate_json(self, system, context, schema, *, images=None):
            paths.extend(images)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    monkeypatch.setattr("app.agents.diagnostics.TEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app.state.generation, "provider_factory", Slow)
    data = client.post("/api/v1/models/test", json={"kind": "vision"}).json()
    assert not data["success"] and data["error"]["code"] == "LLM_TIMEOUT"
    assert cancelled and paths and all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_browser_disconnect_cancels_model_test_and_cleans_temporary_image():
    started, cancelled = asyncio.Event(), asyncio.Event()
    paths = []

    class Slow:
        async def generate_json(self, system, context, schema, *, images=None):
            paths.extend(images)
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
            state=SimpleNamespace(
                config=Settings(_env_file=None, vlm_model="vision"),
                generation=SimpleNamespace(provider_factory=Slow),
            )
        ),
    )
    with pytest.raises(AppError) as failure:
        await model_test_route(request, ModelTestRequest(kind="vision"))
    assert failure.value.code == "REQUEST_CANCELLED" and cancelled.is_set()
    assert paths and all(not path.exists() for path in paths)
