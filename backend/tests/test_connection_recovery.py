import json
from types import SimpleNamespace

import httpx
import pytest

import app.comfyui.client as module
from app.comfyui.client import ComfyUIClient
from app.core.config import Settings
from app.core.errors import AppError
from tests.test_comfyui import noop


def client_for(handler, **settings):
    return ComfyUIClient(
        Settings(_env_file=None, poll_interval=0.01, **settings),
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
        use_websocket=False,
    )


async def test_disconnect_recovers_original_prompt_without_resubmission():
    calls, progress = [], []
    reads = 0

    def handler(request):
        nonlocal reads
        calls.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "original"})
        assert request.url.path == "/history/original"
        reads += 1
        if reads <= 3:
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(200, json={"original": {"status": {"completed": True}}})

    async def record(data):
        progress.append(data)

    async with client_for(handler) as client:
        result = await client.execute({}, "job", noop, record, lambda: False)
    assert result["status"]["completed"]
    assert calls.count(("POST", "/prompt")) == 1
    assert any(p.get("connection") == "reconnecting" for p in progress)
    assert progress[-1]["connection"] == "connected"


async def test_connection_timeout_preserves_prompt_and_does_not_cancel():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("offline", request=request)

    async with client_for(handler, render_timeout=1) as client:
        with pytest.raises(AppError) as error:
            await client.execute({}, "job", noop, noop, lambda: False, prompt_id="known")
    assert error.value.code == "JOB_TIMEOUT"
    assert error.value.details == {"prompt_id": "known"}
    assert set(calls) == {"/history/known"}


async def test_cancel_during_disconnect_checks_original_remote_task():
    stopped = False
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [[0, "known"]]})
        if request.url.path == "/interrupt":
            assert json.loads(request.content) == {"prompt_id": "known"}
            return httpx.Response(200)
        raise httpx.ReadTimeout("offline", request=request)

    async def record(data):
        nonlocal stopped
        if data.get("connection") == "reconnecting":
            stopped = True

    async with client_for(handler) as client:
        with pytest.raises(AppError) as error:
            await client.execute({}, "job", noop, record, lambda: stopped, prompt_id="known")
    assert error.value.code == "CANCELLED"
    assert ("POST", "/interrupt") in calls
    assert ("POST", "/prompt") not in calls


async def test_failed_cancel_stays_uncertain():
    stopped = False

    def handler(request):
        raise httpx.ReadTimeout("offline", request=request)

    async def record(data):
        nonlocal stopped
        stopped = data.get("connection") == "reconnecting"

    async with client_for(handler) as client:
        with pytest.raises(AppError) as error:
            await client.execute({}, "job", noop, record, lambda: stopped, prompt_id="known")
    assert error.value.code == "COMFYUI_OFFLINE"


async def test_download_recovers_without_resubmitting(tmp_path):
    calls, progress = [], []
    target = tmp_path / "output.png"

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(200, content=b"recovered asset")

    async def record(data):
        progress.append(data)

    async with client_for(handler) as client:
        await client.recover_read(
            lambda: client.download({"filename": "output.png"}, target),
            "known",
            record,
            lambda: False,
        )
    assert target.read_bytes() == b"recovered asset"
    assert calls == ["/view", "/view"]
    assert [p["connection"] for p in progress] == ["reconnecting", "connected"]


async def test_execution_error_is_not_retried_as_disconnect():
    def handler(request):
        return httpx.Response(200, json={"known": {"status": {"status_str": "error"}}})

    async with client_for(handler) as client:
        with pytest.raises(AppError) as error:
            await client.execute({}, "job", noop, noop, lambda: False, prompt_id="known")
    assert error.value.code == "EXECUTION_ERROR"


async def test_websocket_reconnects_and_filters_other_tasks(monkeypatch):
    clock, connections, progress = [0], [], []
    reads = 0

    class Socket:
        async def recv(self):
            if len(connections) == 1:
                raise ConnectionError("lost socket")
            return json.dumps(
                {"type": "progress", "data": {"prompt_id": "known", "value": 4, "max": 10}}
            )

        async def close(self):
            pass

    async def connect(url, **kwargs):
        connections.append(url)
        return Socket()

    async def pause(delay, cancelled):
        clock[0] += 6

    def handler(request):
        nonlocal reads
        reads += 1
        return httpx.Response(200, json={"known": {"status": {"completed": reads >= 4}}})

    async def record(data):
        progress.append(data)

    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async with client_for(handler) as client:
        client.use_websocket = True
        client.pause = pause
        await client.execute({}, "job", noop, record, lambda: False, prompt_id="known")
    assert len(connections) == 2
    assert connections[0] == connections[1]
    assert progress[-1]["value"] == 4


def test_pipeline_timeout_then_resume_reuses_original_job(system):
    from tests.test_api_pipeline import wait_episode

    api, app, comfy = system
    comfy.settings.render_timeout = 1
    original = comfy.handle
    offline = True
    reconnect_seen = False

    def handler(request):
        nonlocal reconnect_seen
        if request.url.path.startswith("/history/"):
            prompt = comfy.prompts.get(request.url.path.rsplit("/", 1)[1], {})
            if (
                prompt.get("prompt", {}).get("save", {}).get("class_type") == "SaveVideo"
                and offline
            ):
                jobs = app.state.store.list("job")
                current = next(j for j in jobs if j["status"] == "RUNNING")
                if (current.get("progress") or {}).get("connection") == "reconnecting":
                    reconnect_seen = True
                    episode = app.state.store.get("episode", current["episode_id"])
                    assert episode["status"] == "RENDERING_VIDEO"
                raise httpx.ReadTimeout("offline", request=request)
        return original(request)

    comfy.handle = handler
    episode = api.post(
        "/api/v1/episodes", json={"idea": "reconnect", "target_duration": 1, "qa_enabled": False}
    ).json()
    id = episode["id"]
    assert api.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    failed = wait_episode(api, id)
    assert failed["error"]["code"] == "JOB_TIMEOUT"
    assert reconnect_seen
    job = next(j for j in app.state.store.list("job", id) if j["status"] == "UNKNOWN")
    count = len(comfy.prompts)
    offline = False
    assert api.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    recovered = wait_episode(api, id)
    assert recovered["status"] == "COMPLETED", recovered.get("error")
    assert len(comfy.prompts) == count
    assert app.state.store.get("job", job["id"])["status"] == "COMPLETED"
    assert recovered["plan"] == failed["plan"]
    assert recovered["references"] == failed["references"]
    assert (
        recovered["shots"][0]["start_frame_asset_id"] == failed["shots"][0]["start_frame_asset_id"]
    )
