import asyncio
import time
from copy import deepcopy

import pytest

from app.agents.schemas import ShotPrompts
from tests.fakes import FakeProvider
from tests.test_ai_parameters import CreativeProvider, configure_ai_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_workflows import imported, rebind


def wait_preview(client, id):
    for _ in range(300):
        episode = client.get(f"/api/v1/episodes/{id}").json()
        if episode["status"] in {"AWAITING_REVIEW", "FAILED", "CANCELLED"}:
            return episode
        time.sleep(0.02)
    raise AssertionError("preview timed out")


def preview(system, **extra):
    client, _, _ = system
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Preview fixture",
            "target_duration": 5,
            "preview_required": True,
            "max_shot_duration": 3,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    episode = wait_preview(client, id)
    assert episode["status"] == "AWAITING_REVIEW", episode.get("error")
    return episode


def edits(episode):
    return {
        "expected_version": episode["version"],
        "shots": [
            {
                "id": shot["id"],
                "title": shot["title"],
                "duration": shot["duration"],
                **shot["preview_prompt_view"]["values"],
            }
            for shot in episode["shots"]
        ],
    }


class PromptProvider(CreativeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        result = await super().generate_json(system, context, schema, images=images)
        if issubclass(schema, ShotPrompts):
            mapping = deepcopy(result.ai_parameters)
            for item in context["workflow_parameters"]:
                if item["role"] == "prompt":
                    mapping.setdefault(item["workflow_id"], {})[item["key"]] = "AI role prompt"
            result = schema.model_validate({**result.model_dump(), "ai_parameters": mapping})
        return result


def test_preview_is_render_free_edits_reach_patch_and_approval_reuses_plan(system):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    app.state.generation.provider_factory = PromptProvider
    episode = preview(system)
    id = episode["id"]
    assert len(episode["shots"]) == 2 and episode["bible"]
    assert not episode["references"] and not app.state.store.list("asset", id)
    assert not app.state.store.list("job", id) and not comfy.prompts
    assert all(method == "GET" for method, _ in comfy.calls)
    assert episode["shots"][0]["preview_prompt_view"]["values"]["video_prompt"] == "AI role prompt"
    assert "start_frame_prompt" in episode["shots"][1]["preview_prompt_view"]["locked"]
    body = edits(episode)
    body["shots"][0].update(
        title="Reviewed opening",
        duration=2,
        video_prompt="Reviewed camera pan",
        start_frame_prompt="Reviewed start",
        end_frame_prompt="Reviewed end",
    )
    body["shots"][1]["duration"] = 3
    response = client.patch(f"/api/v1/episodes/{id}/preview", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["plan"]["shots"][0]["duration"] == 2
    assert saved["shots"][0]["title"] == "Reviewed opening"
    assert not comfy.prompts
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": episode["version"]}
        ).status_code
        == 409
    )
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=body).status_code == 409
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": saved["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final["error"]
    assert final["metrics"]["llm_calls"] == saved["metrics"]["llm_calls"]
    assert abs(final["final_duration"] - 5) < 0.1
    assert final["plan"] == saved["plan"]
    jobs = app.state.store.list("job", id)
    first = [job for job in jobs if job["shot_id"] == saved["shots"][0]["id"]]
    for job in first:
        prompt = job["patched_workflow"]["positive"]["inputs"]["text"]
        expected = {
            "SHOT_VIDEO": "Reviewed camera pan",
            "SHOT_START_FRAME": "Reviewed start",
            "SHOT_END_FRAME": "Reviewed end",
        }[job["type"]]
        assert expected in prompt and "AI role prompt" not in prompt
        assert job["patched_workflow"]["sampler"]["inputs"]["denoise"] == (
            0.6 if job["type"] == "SHOT_VIDEO" else 0.4
        )
    assert len(first) == 3
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=edits(saved)).status_code == 409
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 409


@pytest.mark.parametrize(
    "case",
    ["total", "ceiling", "minimum", "duplicate", "missing", "empty", "ai_injection", "locked"],
)
def test_invalid_preview_edits_are_atomic(system, case):
    client, app, comfy = system
    episode = preview(system)
    body = edits(episode)
    if case == "total":
        body["shots"][0]["duration"] = 2
    if case == "ceiling":
        body["shots"][0]["duration"], body["shots"][1]["duration"] = 4, 1
    if case == "minimum":
        body["shots"][0]["duration"] = 0
    if case == "duplicate":
        body["shots"][1]["id"] = body["shots"][0]["id"]
    if case == "missing":
        body["shots"].pop()
    if case == "empty":
        body["shots"][0]["video_prompt"] = " "
    if case == "ai_injection":
        body["shots"][0]["ai_parameters"] = {"system": {"width": 9999}}
    if case == "locked":
        body["shots"][1]["start_frame_prompt"] = "unusable new frame"
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=body)
    assert response.status_code in {400, 422}, response.text
    assert app.state.store.get("episode", episode["id"]) == episode
    assert not comfy.prompts


def test_preview_i2v_omits_end_render_and_honors_fixed_prompt_override(system):
    client, app, comfy = system
    workflow = imported(app, "IMAGE_TO_VIDEO")
    episode = preview(
        system,
        target_duration=1,
        video_workflow_id=workflow["id"],
        workflow_overrides={workflow["id"]: {"positive.text": "User fixed prompt"}},
    )
    id = episode["id"]
    shot = episode["shots"][0]
    assert episode["preview"]["capability"] == "IMAGE_TO_VIDEO"
    assert shot["preview_prompt_view"]["values"]["video_prompt"] == "User fixed prompt"
    assert {"video_prompt", "end_frame_prompt"} <= shot["preview_prompt_view"]["locked"].keys()
    bad = edits(episode)
    bad["shots"][0]["video_prompt"] = "Cannot replace override"
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=bad).status_code == 400
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": episode["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final["error"]
    assert final["shots"][0]["end_frame_asset_id"] is None
    jobs = app.state.store.list("job", id)
    assert not any(job["type"] == "SHOT_END_FRAME" for job in jobs)
    assert (
        next(job for job in jobs if job["type"] == "SHOT_VIDEO")["patched_workflow"]["positive"][
            "inputs"
        ]["text"]
        == "User fixed prompt"
    )


def test_preview_rejects_render_bypasses_and_workflow_drift(system):
    client, app, comfy = system
    episode = preview(system)
    id = episode["id"]
    for path in [
        f"/episodes/{id}/generate",
        f"/episodes/{id}/compose",
        f"/shots/{episode['shots'][0]['id']}/retry-video",
    ]:
        assert client.post("/api/v1" + path).status_code == 409
    assert (
        client.patch(
            f"/api/v1/episodes/{id}/timeline",
            json={"shots": [{"id": s["id"]} for s in episode["shots"]]},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            "/api/v1/workflows/default_video", json={"parameter_values": {"sampler.steps": 11}}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": episode["version"]}
        ).status_code
        == 409
    )
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=edits(episode)).status_code == 409
    assert not comfy.prompts
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    refreshed = wait_preview(client, id)
    assert refreshed["status"] == "AWAITING_REVIEW"
    assert refreshed["shots"][0]["id"] != episode["shots"][0]["id"]
    replacement = imported(app)
    changed = rebind(client, refreshed, image_workflow_id=replacement["id"]).json()
    assert (
        changed["preview_required"]
        and changed["preview"] is None
        and not changed.get("preview_approved_at")
    )
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409


def test_preview_cancel_resume_and_restart_preserve_unapproved_work(system):
    client, app, comfy = system

    class SlowShot(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if issubclass(schema, ShotPrompts):
                await asyncio.sleep(0.15)
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = SlowShot
    created = client.post(
        "/api/v1/episodes",
        json={"idea": "cancel preview", "target_duration": 1, "preview_required": True},
    ).json()
    id = created["id"]
    client.post(f"/api/v1/episodes/{id}/preview")
    for _ in range(200):
        if client.get(f"/api/v1/episodes/{id}").json()["status"] == "PREPARING_PROMPTS":
            break
        time.sleep(0.005)
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 409
    client.post(f"/api/v1/episodes/{id}/cancel")
    client.portal.call(app.state.generation.queue.join)
    assert wait_preview(client, id)["status"] == "CANCELLED"
    assert not comfy.prompts
    app.state.generation.provider_factory = FakeProvider
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    ready = wait_preview(client, id)
    assert ready["status"] == "AWAITING_REVIEW"
    client.portal.call(app.state.generation.stop)
    client.portal.call(app.state.generation.start)
    assert app.state.store.get("episode", id) == ready
    assert not comfy.prompts


def test_rebinding_with_historical_jobs_allows_new_preview(system):
    client, app, comfy = system
    episode = preview(system)
    id = episode["id"]
    app.state.store.create(
        "job",
        {
            "status": "COMPLETED",
            "step_key": "reference:style",
            "profile_snapshot": {"media_type": "image"},
        },
        parent=id,
    )
    replacement = imported(app)
    changed = rebind(client, episode, image_workflow_id=replacement["id"])
    assert changed.status_code == 200, changed.text
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    renewed = wait_preview(client, id)
    assert renewed["status"] == "AWAITING_REVIEW", renewed["error"]
    assert not comfy.prompts


@pytest.mark.parametrize("duration", [60, 90])
def test_long_preview_prepares_every_prompt_without_rendering(system, duration):
    client, app, comfy = system
    episode = preview(system, target_duration=duration)
    assert sum(s["duration"] for s in episode["shots"]) == duration
    assert all(s["prompts"] and 1 <= s["duration"] <= 3 for s in episode["shots"])
    assert not comfy.prompts and not app.state.store.list("job", episode["id"])
    events = client.get(f"/api/v1/episodes/{episode['id']}/events")
    assert events.status_code == 200 and '"AWAITING_REVIEW"' in events.text


def test_fixed_duration_preview_rejects_retiming(system):
    client, app, _ = system
    video = app.state.store.get("workflow", "default_video")
    binding = video["bindings"]["duration"]
    # The bundled profile binds frames: 2 seconds at 16 FPS with offset 1.
    episode = preview(
        system,
        target_duration=4,
        workflow_overrides={"default_video": {f"{binding['node_id']}.{binding['input']}": 33}},
    )
    assert episode["preview"]["fixed_duration"] == 2
    body = edits(episode)
    body["shots"][0]["duration"], body["shots"][1]["duration"] = 1, 3
    assert client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=body).status_code == 400
