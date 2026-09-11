import asyncio
import threading
import time

import pytest

from app.agents.schemas import EpisodePlan, QAResult, ShotPrompts, VisualBible
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode


def create(client, idea):
    return client.post(
        "/api/v1/episodes",
        json={"idea": idea, "target_duration": 1, "width": 256, "height": 256},
    ).json()["id"]


def wait_idle(generation, id):
    deadline = time.monotonic() + 3
    while id in generation.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert id not in generation.busy


@pytest.mark.parametrize("blocked_schema", [EpisodePlan, VisualBible, ShotPrompts, QAResult])
def test_cancel_model_wait_drains_cleanup_before_allowing_delete(system, blocked_schema):
    client, app, comfy = system
    entered, cleaning, release = (threading.Event() for _ in range(3))

    class BlockedProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if issubclass(schema, blocked_schema):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    assert await asyncio.to_thread(release.wait, 5)
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = BlockedProvider
    id = create(client, "cancel blocked model")
    client.post(f"/api/v1/episodes/{id}/generate")
    try:
        assert entered.wait(3)
        before = app.state.store.get("episode", id)
        assets = app.state.store.list("asset", id)
        submissions = len(comfy.prompts)
        assert client.post(f"/api/v1/episodes/{id}/cancel").json()["status"] == "CANCELLED"
        assert cleaning.wait(2), "Cancellation must not wait for the model timeout"
        blocked = client.delete(f"/api/v1/episodes/{id}")
        assert blocked.status_code == 409
        assert "正在停止后台任务" in blocked.json()["error"]["message"]
        assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    finally:
        release.set()
    wait_idle(app.state.generation, id)
    after = app.state.store.get("episode", id)
    assert after["status"] == "CANCELLED"
    for key in ["plan", "bible", "shots", "references"]:
        assert after[key] == before[key]
    assert len(comfy.prompts) == submissions
    assert app.state.store.list("asset", id) == assets
    assert client.delete(f"/api/v1/episodes/{id}").status_code == 204
    assert client.get(f"/api/v1/episodes/{id}").status_code == 404
    assert app.state.store.list("asset", id) == assets
    assert app.state.generation.worker and not app.state.generation.worker.done()


def test_cancel_queued_episode_allows_delete_without_waiting_for_other_episode(system):
    client, app, _ = system
    entered, release = threading.Event(), threading.Event()

    class FirstBlocked(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if issubclass(schema, EpisodePlan) and context["idea"] == "first blocked":
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = FirstBlocked
    first, removed, last = [create(client, idea) for idea in ["first blocked", "removed", "last"]]
    client.post(f"/api/v1/episodes/{first}/generate")
    try:
        assert entered.wait(3)
        for id in [removed, last]:
            assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
        assert client.post(f"/api/v1/episodes/{removed}/cancel").json()["status"] == "CANCELLED"
        assert client.delete(f"/api/v1/episodes/{removed}").status_code == 204
        assert first in app.state.generation.busy and last in app.state.generation.busy
    finally:
        release.set()
    for id in [first, last]:
        assert wait_episode(client, id)["status"] == "COMPLETED"
    assert client.get(f"/api/v1/episodes/{removed}").status_code == 404
    assert app.state.store.list("job", removed) == []
    assert app.state.generation.queue.empty()


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "UNKNOWN"])
def test_cancel_does_not_unlock_delete_with_unsettled_render_job(system, state):
    client, app, _ = system
    id = create(client, "unsettled render")
    app.state.store.update("episode", id, {"status": "CANCELLED"})
    job = app.state.store.create("job", {"status": state, "type": "SHOT_VIDEO"}, parent=id)
    before = app.state.store.get("episode", id)
    client.post(f"/api/v1/episodes/{id}/cancel")
    response = client.delete(f"/api/v1/episodes/{id}?delete_assets=true")
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"jobs": [{"id": job["id"], "status": state}]}
    assert app.state.store.get("episode", id) == before
    assert app.state.store.get("job", job["id"]) == job


def test_cancel_read_only_preflight_ends_wait_without_submitting_render(system, monkeypatch):
    client, app, comfy = system
    entered, stopped = threading.Event(), threading.Event()

    async def blocked_system(self):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("app.comfyui.client.ComfyUIClient.system", blocked_system)
    id = create(client, "cancel preflight")
    client.post(f"/api/v1/episodes/{id}/generate")
    assert entered.wait(3)
    assert client.post(f"/api/v1/episodes/{id}/cancel").json()["status"] == "CANCELLED"
    assert stopped.wait(2)
    wait_idle(app.state.generation, id)
    assert not comfy.prompts and not app.state.store.list("job", id)
    assert client.delete(f"/api/v1/episodes/{id}").status_code == 204
