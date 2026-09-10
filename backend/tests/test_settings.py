import asyncio
import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AppError
from app.core.runtime_settings import RuntimeSettings
from app.main import create_app


def configuration(tmp_path, **kwargs):
    return Settings(_env_file=None, data_dir=tmp_path, **kwargs)


def test_online_config_is_live_persistent_and_never_returns_secrets(tmp_path):
    base = configuration(tmp_path, llm_model="environment-model")
    app = create_app(base)
    with TestClient(app) as client:
        assert client.get("/api/v1/settings").json()["llm_timeout"] == 600
        provider = app.state.generation.provider_factory()
        result = client.patch(
            "/api/v1/settings",
            json={
                "comfyui_url": "http://127.0.0.1:8288",
                "llm_base_url": "https://models.example/v1/",
                "llm_model": "online-director",
                "vlm_model": "online-vision",
                "llm_timeout": 900,
                "llm_api_key": "test-secret-never-return",
                "render_timeout": 900,
                "max_asset_mb": 128,
                "request_timeout": 20,
                "poll_interval": 0.5,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["llm_api_key_configured"] is True
        for response in (result, client.get("/api/v1/settings"), client.get("/openapi.json")):
            assert "test-secret-never-return" not in response.text
        assert "llm_api_key" not in result.json()
        assert app.state.engine.settings.render_timeout == 900
        assert app.state.engine.client().url == "http://127.0.0.1:8288"
        assert app.state.generation.settings.vlm_model == "online-vision"
        assert provider.settings.llm_model == "online-director"
        assert provider.settings.llm_timeout == 900
        assert base.llm_model == "environment-model"

        class Probe(BaseModel):
            ok: bool

        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

        provider.transport = httpx.MockTransport(handler)
        assert asyncio.run(provider.generate_json("probe", {}, Probe)).ok
        assert str(calls[0].url) == "https://models.example/v1/chat/completions"
        assert calls[0].headers["authorization"] == "Bearer test-secret-never-return"
        assert json.loads(calls[0].content)["model"] == "online-director"
        assert calls[0].extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 900
        )
        assert "test-secret-never-return" not in repr(app.state.config)
    path = tmp_path / "runtime-settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with TestClient(create_app(configuration(tmp_path, llm_model="changed-environment"))) as client:
        result = client.get("/api/v1/settings").json()
        assert result["llm_model"] == "online-director"
        assert result["llm_timeout"] == 900
        assert result["comfyui_url"] == "http://127.0.0.1:8288"
        assert result["llm_api_key_configured"] is True


def test_secret_omission_endpoint_change_and_explicit_clear(tmp_path):
    base = configuration(
        tmp_path, llm_api_key="environment-secret", llm_base_url="https://first.example/v1/"
    )
    with TestClient(create_app(base)) as client:
        for payload in ({"llm_model": "first"}, {"llm_api_key": ""}, {"llm_api_key": None}):
            response = client.patch("/api/v1/settings", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["llm_api_key_configured"] is True
        response = client.patch(
            "/api/v1/settings", json={"llm_base_url": "https://second.example/v1"}
        )
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "CREDENTIAL_REQUIRED"
        )
        response = client.patch(
            "/api/v1/settings",
            json={
                "llm_base_url": "https://second.example/v1",
                "clear_llm_api_key": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["llm_api_key_configured"] is False
    with TestClient(create_app(base)) as client:
        assert client.get("/api/v1/settings").json()["llm_api_key_configured"] is False
        assert (
            client.patch(
                "/api/v1/settings",
                json={
                    "clear_llm_api_key": True,
                    "llm_api_key": "conflicting-key",
                },
            ).status_code
            == 422
        )
        replaced = client.patch("/api/v1/settings", json={"llm_api_key": "replacement-key"})
        assert replaced.json()["llm_api_key_configured"] is True
        assert "replacement-key" not in replaced.text


@pytest.mark.parametrize(
    "payload",
    [
        {"llm_base_url": "file:///etc/passwd"},
        {"comfyui_url": "http://["},
        {"comfyui_url": "http://localhost:99999"},
        {"comfyui_url": "https://user:secret@localhost"},
        {"llm_base_url": "https://user:secret@host/v1"},
        {"llm_base_url": "https://host/v1?api_key=secret"},
        {"render_timeout": 0},
        {"llm_timeout": 0},
        {"llm_timeout": 3601},
        {"llm_timeout": None},
        {"llm_timeout": "nan"},
        {"llm_timeout": "inf"},
        {"max_asset_mb": 2049},
        {"llm_model": None},
        {"data_dir": "/tmp/move"},
        {"llm_api_key": "invalid\nkey"},
    ],
)
def test_invalid_online_configuration_is_atomic(tmp_path, payload):
    with TestClient(create_app(configuration(tmp_path))) as client:
        before = client.get("/api/v1/settings").json()
        response = client.patch("/api/v1/settings", json=payload)
        assert response.status_code == 422, response.text
        assert client.get("/api/v1/settings").json() == before
        assert not (tmp_path / "runtime-settings.json").exists()


@pytest.mark.parametrize("state", ["episode", "unknown", "cancelling", "render-lock"])
def test_configuration_cannot_change_during_work(tmp_path, state):
    app = create_app(configuration(tmp_path))
    with TestClient(app) as client:
        if state == "episode":
            app.state.store.create("episode", {"status": "PLANNING"})
        elif state == "unknown":
            app.state.store.create("job", {"status": "UNKNOWN"})
        elif state == "cancelling":
            app.state.generation.busy.add("cancelled-but-still-exiting")
        else:
            asyncio.run(app.state.engine.lock.acquire())
        try:
            result = client.patch("/api/v1/settings", json={"llm_model": "changed"})
            assert result.status_code == 409
            assert app.state.config.llm_model == ""
            assert not (tmp_path / "runtime-settings.json").exists()
        finally:
            if state == "render-lock":
                app.state.engine.lock.release()


def test_settings_recheck_busy_after_url_validation(tmp_path, monkeypatch):
    app = create_app(configuration(tmp_path))
    with TestClient(app) as client:

        async def enqueue_during_dns(url, allow_public):
            app.state.generation.busy.add("new-job")
            return url

        monkeypatch.setattr("app.core.runtime_settings.validate_comfy_url", enqueue_during_dns)
        assert (
            client.patch(
                "/api/v1/settings", json={"comfyui_url": "http://127.0.0.1:8288"}
            ).status_code
            == 409
        )
        assert app.state.config.comfyui_url == "http://127.0.0.1:8188"


def test_save_failure_does_not_publish_or_leave_temporary_secret_files(tmp_path, monkeypatch):
    with TestClient(create_app(configuration(tmp_path))) as client:

        def fail(*args):
            raise OSError("fixture write failure")

        monkeypatch.setattr("app.core.runtime_settings.os.replace", fail)
        response = client.patch("/api/v1/settings", json={"llm_api_key": "unsaved-key"})
        assert response.status_code == 500
        assert "unsaved-key" not in response.text
        assert client.get("/api/v1/settings").json()["llm_api_key_configured"] is False
        assert not list(tmp_path.glob(".settings-*"))
        assert not (tmp_path / "runtime-settings.json").exists()


def test_legacy_comfy_url_and_invalid_file_handling(tmp_path):
    base = configuration(tmp_path)
    runtime = RuntimeSettings(base, {"comfyui_url": "http://127.0.0.1:8288"})
    assert runtime.public()["comfyui_url"] == "http://127.0.0.1:8288"
    runtime.path.write_text('{"unknown": "private-value"}')
    with pytest.raises(AppError) as failure:
        RuntimeSettings(configuration(tmp_path), {})
    assert failure.value.code == "CONFIGURATION_INVALID"
    assert "private-value" not in str(failure.value)


def test_older_saved_configuration_inherits_model_timeout_default(tmp_path):
    (tmp_path / "runtime-settings.json").write_text('{"llm_model":"saved-model"}')
    with TestClient(create_app(configuration(tmp_path))) as client:
        settings = client.get("/api/v1/settings").json()
        assert settings["llm_model"] == "saved-model"
        assert settings["llm_timeout"] == 600


def test_legacy_url_credentials_are_not_echoed_to_the_browser(tmp_path):
    base = configuration(
        tmp_path, llm_base_url="https://user:legacy-secret@models.example/v1?api_key=query-secret"
    )
    with TestClient(create_app(base)) as client:
        response = client.get("/api/v1/settings")
        assert response.json()["llm_base_url"] == "https://models.example/v1"
        assert "legacy-secret" not in response.text and "query-secret" not in response.text
