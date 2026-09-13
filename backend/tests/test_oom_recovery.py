from copy import deepcopy

import httpx
import pytest

from app.core.errors import AppError
from tests.media_assertions import assert_full_duration
from tests.test_api_pipeline import wait_episode


def interrupted(system, suffix, oom_count=1):
    client, app, comfy = system
    comfy.settings.render_timeout = 1
    original = comfy.handle
    state = {"offline": True, "videos": [], "job_id": None}

    def handler(request):
        path = request.url.path
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            body = comfy.prompts.get(prompt_id)
            if body:
                job = app.state.store.get("job", body["client_id"])
                if prompt_id in state["videos"][:oom_count]:
                    return httpx.Response(
                        200,
                        json={
                            prompt_id: {
                                "status": {
                                    "status_str": "error",
                                    "messages": [
                                        ["execution_error", {"exception_type": "OutOfMemoryError"}]
                                    ],
                                }
                            }
                        },
                    )
                if job["step_key"].endswith(suffix):
                    state["job_id"] = job["id"]
                    if state["offline"]:
                        raise httpx.ReadTimeout("temporary disconnect", request=request)
        result = original(request)
        if path == "/prompt":
            prompt_id = result.json()["prompt_id"]
            if comfy.prompts[prompt_id]["prompt"]["save"]["class_type"] == "SaveVideo":
                state["videos"].append(prompt_id)
        return result

    comfy.handle = handler
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Resume interrupted OOM branch",
            "target_duration": 5,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
            "max_retries": 2,
        },
    ).json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    failed = wait_episode(client, id)
    assert failed["error"]["code"] == "JOB_TIMEOUT", failed.get("error")
    return failed, state


@pytest.mark.parametrize(
    "suffix,oom_count,expected_videos",
    [
        (":video:small", 1, 2),
        (":segment:0", 2, 5),
        (":segment:1", 2, 5),
        (":middle:0", 2, 5),
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_recover_oom_steps_without_resubmitting_known_work(
    system, suffix, oom_count, expected_videos, legacy
):
    client, app, comfy = system
    failed, state = interrupted(system, suffix, oom_count)
    id = failed["id"]
    pending = app.state.store.get("job", state["job_id"])
    old_jobs = app.state.store.list("job", id)
    old_prompts = deepcopy(comfy.prompts)
    if legacy:
        app.state.store.update("episode", id, lambda e: e["shots"][0].pop("render_cursor"))
        # The former broken recovery may have left an extra, never-submitted job.
        full = next(j for j in old_jobs if j["step_key"].endswith(":video"))
        app.state.store.create(
            "job",
            {
                **full,
                "status": "FAILED",
                "comfy_prompt_id": None,
                "error": {"code": "UNRESOLVED_JOB", "message": "old recovery blocked"},
            },
            parent=id,
        )
    client.portal.call(app.state.generation.stop)
    client.portal.call(app.state.generation.start)
    state["offline"] = False
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    recovered = wait_episode(client, id)
    assert recovered["status"] == "COMPLETED", recovered.get("error")
    assert len(state["videos"]) == expected_videos
    for prompt_id, body in old_prompts.items():
        assert comfy.prompts[prompt_id] == body
        assert sum(p["client_id"] == body["client_id"] for p in comfy.prompts.values()) == 1
    job = app.state.store.get("job", pending["id"])
    assert job["status"] == "COMPLETED"
    assert job["comfy_prompt_id"] == pending["comfy_prompt_id"]
    assert job["patched_workflow"] == pending["patched_workflow"]
    assert recovered["plan"] == failed["plan"] and recovered["references"] == failed["references"]
    for role in ("start_frame_asset_id", "end_frame_asset_id"):
        assert recovered["shots"][0][role] == failed["shots"][0][role]
    assert_full_duration(app, recovered)
    assert not recovered.get("render_recovery") and not recovered["shots"][0].get("render_cursor")


def test_second_disconnect_keeps_original_prompt_and_retry_budget(system):
    client, app, comfy = system
    failed, state = interrupted(system, ":video:small")
    id = failed["id"]
    count = len(comfy.prompts)
    client.post(f"/api/v1/episodes/{id}/generate")
    again = wait_episode(client, id)
    assert again["error"]["code"] == "JOB_TIMEOUT"
    assert len(comfy.prompts) == count
    assert again["render_recovery"]["cursor"]["remaining"] == 2
    state["offline"] = False
    client.post(f"/api/v1/episodes/{id}/generate")
    assert wait_episode(client, id)["status"] == "COMPLETED"
    assert len(comfy.prompts) == count


def test_missing_prompt_id_or_foreign_unknown_preserves_protection(system):
    client, app, comfy = system
    failed, state = interrupted(system, ":video:small")
    id = failed["id"]
    count = len(comfy.prompts)
    app.state.store.update("job", state["job_id"], {"comfy_prompt_id": None})
    client.post(f"/api/v1/episodes/{id}/generate")
    result = wait_episode(client, id)
    assert result["error"]["code"] == "SUBMISSION_UNKNOWN"
    other = client.post(
        "/api/v1/episodes", json={"idea": "another episode", "target_duration": 1}
    ).json()["id"]
    client.post(f"/api/v1/episodes/{other}/generate")
    assert wait_episode(client, other)["error"]["code"] == "UNRESOLVED_JOB"
    assert len(comfy.prompts) == count
    assert app.state.store.get("job", state["job_id"])["status"] == "UNKNOWN"


def test_recovered_job_survives_local_postprocessing_failure(system, monkeypatch):
    import app.generation.pipeline as pipeline

    client, app, comfy = system
    failed, state = interrupted(system, ":video:small")
    state["offline"] = False
    original = pipeline.extract_frame

    async def fail_once(*args, **kwargs):
        monkeypatch.setattr(pipeline, "extract_frame", original)
        raise AppError("COMPOSE_FAILED", "temporary local postprocessing failure")

    monkeypatch.setattr(pipeline, "extract_frame", fail_once)
    count = len(comfy.prompts)
    client.post(f"/api/v1/episodes/{failed['id']}/generate")
    again = wait_episode(client, failed["id"])
    assert again["error"]["code"] == "COMPOSE_FAILED"
    assert app.state.store.get("job", state["job_id"])["status"] == "COMPLETED"
    client.post(f"/api/v1/episodes/{failed['id']}/generate")
    assert wait_episode(client, failed["id"])["status"] == "COMPLETED"
    assert len(comfy.prompts) == count


def test_recovery_uses_job_profile_when_saved_workflow_changes(system):
    client, app, comfy = system
    failed, state = interrupted(system, ":video:small")
    profile = client.get("/api/v1/workflows/default_video").json()
    response = client.patch(
        "/api/v1/workflows/default_video",
        json={
            "capabilities": {**profile["capabilities"], "max_duration": 1},
            "parameter_values": {"sampler.steps": 12},
        },
    )
    assert response.status_code == 200, response.text
    count = len(comfy.prompts)
    state["offline"] = False
    client.post(f"/api/v1/episodes/{failed['id']}/generate")
    recovered = wait_episode(client, failed["id"])
    assert recovered["status"] == "COMPLETED", recovered.get("error")
    assert len(comfy.prompts) == count
    job = app.state.store.get("job", state["job_id"])
    assert job["patched_workflow"]["sampler"]["inputs"]["steps"] != 12
