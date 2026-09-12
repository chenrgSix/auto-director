"""Fail unusable workflows before spending on media; preserve submitted recovery work."""

from copy import deepcopy

import pytest

from app.core.errors import AppError
from tests.test_api_pipeline import wait_episode
from tests.test_episode_workflows import create, imported
from tests.test_rendered_story_rebind import fail_after_frames


def invalidate_video(comfy, failure):
    if failure == "node":
        del comfy.info["SaveVideo"]
        return "save", "MISSING_NODE"
    if failure == "model":
        comfy.info["UNETLoader"]["input"]["required"]["unet_name"] = [
            ["different-model.safetensors"],
            {},
        ]
        return "model.unet_name", "MISSING_MODEL"
    # The image workflow's 24 steps remain valid; only the video's 30 steps fail.
    comfy.info["KSampler"]["input"]["required"]["steps"] = [
        "INT",
        {"min": 1, "max": 26},
    ]
    return "sampler.steps", "WORKFLOW_INVALID"


def assert_no_submission(comfy):
    assert not comfy.prompts
    assert ("POST", "/prompt") not in comfy.calls
    assert ("POST", "/upload/image") not in comfy.calls


@pytest.mark.parametrize("failure", ["node", "model", "steps"])
def test_video_dependency_failure_precedes_references_and_keyframes(system, failure):
    client, app, comfy = system
    episode = create(client, target_duration=1, qa_enabled=False)
    field, code = invalidate_video(comfy, failure)
    response = client.post(f"/api/v1/episodes/{episode['id']}/generate")
    assert response.status_code == 202, response.text
    failed = wait_episode(client, episode["id"])
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == "WORKFLOW_INVALID"
    assert field in failed["error"]["message"]
    assert any(issue["code"] == code for issue in failed["error"]["details"])
    assert failed["plan"] is None and not failed["shots"]
    assert not app.state.store.list("asset", episode["id"])
    assert not app.state.store.list("job", episode["id"])
    assert_no_submission(comfy)


@pytest.mark.parametrize("selection", ["image_workflow_id", "reference_workflow_id"])
def test_selected_image_and_reference_workflows_are_checked_before_render(system, selection):
    client, app, comfy = system
    profile = imported(app)
    episode = create(client, target_duration=1, **{selection: profile["id"]})
    graph = deepcopy(profile["workflow"])
    graph["save"]["class_type"] = "UnavailableImageWriter"
    app.state.store.update("workflow", profile["id"], {"workflow": graph})
    response = client.post(f"/api/v1/episodes/{episode['id']}/generate")
    assert response.status_code == 202, response.text
    failed = wait_episode(client, episode["id"])
    assert failed["status"] == "FAILED"
    assert profile["name"] in failed["error"]["message"]
    assert any(issue["code"] == "MISSING_NODE" for issue in failed["error"]["details"])
    assert not app.state.store.list("job", episode["id"])
    assert_no_submission(comfy)


def test_valid_advanced_override_replaces_invalid_saved_default_through_submission(system):
    client, app, comfy = system
    app.state.store.update(
        "workflow", "default_video", {"parameter_values": {"sampler.steps": 999}}
    )
    comfy.info["KSampler"]["input"]["required"]["steps"] = [
        "INT",
        {"min": 1, "max": 40},
    ]
    episode = create(
        client,
        target_duration=1,
        width=256,
        height=256,
        qa_enabled=False,
        advanced_mode=True,
        workflow_overrides={"default_video": {"sampler.steps": 20}},
    )
    profile_before = app.state.store.get("workflow", "default_video")
    assert client.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 202
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    video = next(
        job for job in app.state.store.list("job", episode["id"]) if job["type"] == "SHOT_VIDEO"
    )
    assert video["patched_workflow"]["sampler"]["inputs"]["steps"] == 20
    assert video["parameter_sources"]["sampler.steps"] == "user"
    assert comfy.prompts[video["comfy_prompt_id"]]["prompt"] == video["patched_workflow"]
    assert app.state.store.get("workflow", "default_video") == profile_before


def test_video_only_resume_ignores_unneeded_image_dependencies_and_preserves_frames(
    system, monkeypatch
):
    client, app, comfy = system
    failed = fail_after_frames(system, monkeypatch, target_duration=1, qa_enabled=False)
    before = deepcopy(failed)
    jobs_before = {job["id"] for job in app.state.store.list("job", failed["id"])}
    submissions = len(comfy.prompts)
    del comfy.info["SaveImage"]
    assert client.post(f"/api/v1/episodes/{failed['id']}/generate").status_code == 202
    final = wait_episode(client, failed["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert len(comfy.prompts) == submissions + 1
    for key in ("plan", "bible", "references"):
        assert final[key] == before[key]
    for key in ("start_frame_asset_id", "end_frame_asset_id", "prompts", "duration"):
        assert final["shots"][0][key] == before["shots"][0][key]
    new_jobs = [
        job for job in app.state.store.list("job", failed["id"]) if job["id"] not in jobs_before
    ]
    assert [job["type"] for job in new_jobs] == ["SHOT_VIDEO"]


def uploaded_frame(client, comfy):
    response = client.post(
        "/api/v1/assets", files={"file": ("frame.png", comfy.image_bytes, "image/png")}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def video_job(app, start, end):
    return app.state.engine.create_job(
        app.state.store.get("workflow", "default_video"),
        {"prompt": "preflight fixture", "duration": 1, "fps": 16, "width": 256, "height": 256},
        {"start_frame": start, "end_frame": end},
        "workflow-tests",
        None,
        "WORKFLOW_TEST",
    )


@pytest.mark.parametrize("failure", ["node", "model", "steps"])
def test_engine_rechecks_live_dependencies_before_uploading_either_frame(system, failure):
    client, app, comfy = system
    frame = uploaded_frame(client, comfy)
    job = video_job(app, frame, frame)
    field, code = invalidate_video(comfy, failure)
    with pytest.raises(AppError) as error:
        client.portal.call(app.state.engine.run, job["id"])
    assert error.value.code == "WORKFLOW_INVALID"
    assert field in error.value.message
    assert any(issue["code"] == code for issue in error.value.details)
    saved = app.state.store.get("job", job["id"])
    assert saved["status"] == "FAILED" and saved["comfy_prompt_id"] is None
    assert saved["patched_workflow"] is None
    assert_no_submission(comfy)


def test_invalid_second_asset_does_not_upload_valid_first_asset(system):
    client, app, comfy = system
    frame = uploaded_frame(client, comfy)
    response = client.post(
        "/api/v1/assets", files={"file": ("video.mp4", comfy.video_bytes, "video/mp4")}
    )
    assert response.status_code == 201, response.text
    job = video_job(app, frame, response.json()["id"])
    with pytest.raises(AppError) as error:
        client.portal.call(app.state.engine.run, job["id"])
    assert error.value.code == "INVALID_MEDIA"
    assert "end_frame" in error.value.message
    assert app.state.store.get("job", job["id"])["status"] == "FAILED"
    assert_no_submission(comfy)


def test_unknown_job_recovers_saved_graph_without_new_metadata_upload_or_submission(system):
    client, app, comfy = system
    frame = uploaded_frame(client, comfy)
    job = video_job(app, frame, frame)
    client.portal.call(app.state.engine.run, job["id"])
    submitted = app.state.store.get("job", job["id"])
    app.state.store.update("job", job["id"], {"status": "UNKNOWN", "output_asset_ids": []})
    comfy.info.clear()  # A changed server environment cannot invalidate already submitted work.
    comfy.calls.clear()
    prompts_before = deepcopy(comfy.prompts)
    records = client.portal.call(app.state.engine.run, job["id"])
    recovered = app.state.store.get("job", job["id"])
    assert records and recovered["status"] == "COMPLETED"
    assert recovered["comfy_prompt_id"] == submitted["comfy_prompt_id"]
    assert recovered["patched_workflow"] == submitted["patched_workflow"]
    assert comfy.prompts == prompts_before
    assert ("GET", "/object_info") not in comfy.calls
    assert ("POST", "/upload/image") not in comfy.calls
    assert ("POST", "/prompt") not in comfy.calls
    assert ("GET", f"/history/{submitted['comfy_prompt_id']}") in comfy.calls
