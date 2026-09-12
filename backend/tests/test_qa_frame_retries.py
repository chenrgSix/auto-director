"""QA retries repair actual rejected renders without discarding reviewed story or assets."""

from copy import deepcopy

import httpx
import pytest

from app.agents.schemas import EpisodePlan, QAResult, Transition
from app.core.errors import AppError
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import preview
from tests.test_episode_workflows import imported

CORRECTION = "Remove the human hands and oven; preserve the straw nest and locked dawn framing."


def install_qa(
    system,
    *,
    failures=1,
    shot_index=0,
    failed_frame="end_frame",
    legacy=False,
    continuous=False,
):
    _, app, _ = system
    state = {"failures": failures, "checks": 0, "checked_starts": []}

    class FrameQAProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            result = await super().generate_json(system, context, schema, images=images)
            if issubclass(schema, EpisodePlan) and not continuous:
                # Independent scenes make preservation of the second shot's good start observable.
                for shot in result.shots[1:]:
                    shot.transition_from_previous = Transition.TIME_CUT
            if (
                schema is QAResult
                and context["stage"] == "keyframes"
                and context["shot"]["index"] == shot_index
            ):
                state["checks"] += 1
                state["checked_starts"].append(images[0])
                if state["failures"] is None or state["checks"] <= state["failures"]:
                    result = QAResult.model_validate(
                        {
                            **result.model_dump(),
                            "character_consistency": 0.1,
                            "explanation": "The end has unwanted hands; the starting frame is correct.",
                            **(
                                {}
                                if legacy
                                else {
                                    "failed_frames": [failed_frame],
                                    "frame_corrections": {failed_frame: CORRECTION},
                                }
                            ),
                        }
                    )
            return result

    app.state.generation.provider_factory = FrameQAProvider
    return state


def prepared(system, **extra):
    return preview(
        system,
        **{
            "idea": "A chick hatches in a straw nest under soft dawn light",
            "target_duration": 1,
            "max_shot_duration": 1,
            "max_retries": 1,
            "qa_enabled": True,
            **extra,
        },
    )


def approve(client, episode):
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/approve",
        json={"expected_version": episode["version"]},
    )
    assert response.status_code == 202, response.text
    return wait_episode(client, episode["id"])


def shot_jobs(app, episode, index=0, kind=None):
    shot_id = episode["shots"][index]["id"]
    return [
        job
        for job in app.state.store.list("job", episode["id"])
        if job["shot_id"] == shot_id and (kind is None or job["type"] == kind)
    ]


def positive(job):
    return job["patched_workflow"]["positive"]["inputs"]["text"]


def test_tail_qa_retry_keeps_good_start_and_corrects_render_without_editing_review(system):
    client, app, _ = system
    install_qa(system)
    reviewed = prepared(system)
    prompts = deepcopy(reviewed["shots"][0]["prompts"])
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    starts = shot_jobs(app, completed, kind="SHOT_START_FRAME")
    ends = shot_jobs(app, completed, kind="SHOT_END_FRAME")
    assert len(starts) == 1 and len(ends) == 2
    assert len(shot_jobs(app, completed, kind="SHOT_VIDEO")) == 1
    assert completed["shots"][0]["start_frame_asset_id"] in starts[0]["output_asset_ids"]
    assert sum(CORRECTION in positive(job) for job in ends) == 1
    assert all(prompts["end_frame_prompt"] in positive(job) for job in ends)
    assert all(CORRECTION not in positive(job) for job in starts)
    assert completed["shots"][0]["prompts"] == prompts
    assert completed["plan"] == reviewed["plan"]


@pytest.mark.parametrize("legacy", [False, True])
def test_continue_after_exhausted_qa_creates_new_render_preserving_story_and_history(
    system, legacy
):
    client, app, _ = system
    state = install_qa(system, failures=None, shot_index=1, legacy=legacy)
    reviewed = prepared(system, target_duration=2)
    failed = approve(client, reviewed)
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "QA_FAILED"
    first = deepcopy(failed["shots"][0])
    old_jobs = shot_jobs(app, failed, 1)
    old_ids = {job["id"] for job in old_jobs}
    old_seeds = {
        job["patched_workflow"]["sampler"]["inputs"]["seed"]
        for job in old_jobs
        if job["type"] == "SHOT_END_FRAME"
    }
    old_assets = {
        asset["id"]: client.get(f"/api/v1/assets/{asset['id']}/file").content
        for asset in app.state.store.list("asset", failed["id"])
    }
    if legacy:
        # Existing failures predate the persisted retry decision. Reinspect the retained
        # frame once, then accept the newly rendered replacement on the next QA call.
        def old_record(episode):
            shot = episode["shots"][1]
            for field in ("qa_retry", "qa_frame_corrections", "render_cursor"):
                shot.pop(field, None)

        app.state.store.update("episode", failed["id"], old_record)
        state["failures"] = state["checks"] + 1
    else:
        state["failures"] = 0
    response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
    assert response.status_code == 202, response.text
    completed = wait_episode(client, failed["id"])
    assert completed["status"] == "COMPLETED", completed.get("error")
    new_jobs = [job for job in shot_jobs(app, completed, 1) if job["id"] not in old_ids]
    new_ends = [job for job in new_jobs if job["type"] == "SHOT_END_FRAME"]
    assert len(new_ends) == 1
    assert new_ends[0]["patched_workflow"]["sampler"]["inputs"]["seed"] not in old_seeds
    assert len([job for job in new_jobs if job["type"] == "SHOT_START_FRAME"]) == int(legacy)
    if not legacy:
        assert (
            completed["shots"][1]["start_frame_asset_id"]
            == failed["shots"][1]["start_frame_asset_id"]
        )
    assert completed["shots"][0]["video_asset_id"] == first["video_asset_id"]
    assert completed["shots"][0]["start_frame_asset_id"] == first["start_frame_asset_id"]
    assert len(shot_jobs(app, completed, kind="SHOT_VIDEO")) == 1
    assert completed["plan"] == reviewed["plan"]
    assert [shot["prompts"] for shot in completed["shots"]] == [
        shot["prompts"] for shot in reviewed["shots"]
    ]
    for asset_id, content in old_assets.items():
        response = client.get(f"/api/v1/assets/{asset_id}/file")
        assert response.status_code == 200 and response.content == content
    for job in old_jobs:
        assert app.state.store.get("job", job["id"]) == job


def test_i2v_qa_retry_repairs_start_without_end_render(system):
    client, app, _ = system
    install_qa(system, failed_frame="start_frame")
    workflow = imported(app, "IMAGE_TO_VIDEO")
    reviewed = prepared(system, video_workflow_id=workflow["id"])
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    starts = shot_jobs(app, completed, kind="SHOT_START_FRAME")
    assert len(starts) == 2
    assert sum(CORRECTION in positive(job) for job in starts) == 1
    assert not shot_jobs(app, completed, kind="SHOT_END_FRAME")
    assert completed["shots"][0]["end_frame_asset_id"] is None


@pytest.mark.parametrize("interrupted_corrected_tail", [False, True])
def test_legacy_qa_reinspection_does_not_spend_fresh_render_budget(
    system, interrupted_corrected_tail
):
    client, app, comfy = system
    install_qa(system, failures=2)
    state = {"offline": interrupted_corrected_tail, "ends": [], "job_id": None}
    if interrupted_corrected_tail:
        comfy.settings.render_timeout = 1
        original = comfy.handle

        def handle(request):
            path = request.url.path
            if path.startswith("/history/") and len(state["ends"]) >= 2:
                prompt_id = path.rsplit("/", 1)[1]
                if prompt_id == state["ends"][1]:
                    state["job_id"] = comfy.prompts[prompt_id]["client_id"]
                    if state["offline"]:
                        raise httpx.ReadTimeout("Legacy correction disconnected", request=request)
            response = original(request)
            if path == "/prompt":
                prompt_id = response.json()["prompt_id"]
                job = app.state.store.get("job", comfy.prompts[prompt_id]["client_id"])
                if job["type"] == "SHOT_END_FRAME":
                    state["ends"].append(prompt_id)
            return response

        comfy.handle = handle
    reviewed = prepared(system, max_retries=0)
    failed = approve(client, reviewed)
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "QA_FAILED"
    first_start = failed["shots"][0]["start_frame_asset_id"]
    old_end = failed["shots"][0]["end_frame_asset_id"]

    def old_record(episode):
        for field in ("qa_retry", "qa_frame_corrections", "render_cursor"):
            episode["shots"][0].pop(field, None)

    app.state.store.update("episode", failed["id"], old_record)
    response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
    assert response.status_code == 202, response.text
    completed = wait_episode(client, failed["id"])
    if interrupted_corrected_tail:
        assert completed["error"]["code"] == "JOB_TIMEOUT", completed.get("error")
        pending = app.state.store.get("job", state["job_id"])
        assert pending["status"] == "UNKNOWN" and CORRECTION in positive(pending)
        submitted = deepcopy(comfy.prompts)
        state["offline"] = False
        response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
        assert response.status_code == 202, response.text
        completed = wait_episode(client, failed["id"])
        recovered = app.state.store.get("job", pending["id"])
        assert recovered["status"] == "COMPLETED"
        assert recovered["comfy_prompt_id"] == pending["comfy_prompt_id"]
        assert recovered["patched_workflow"] == pending["patched_workflow"]
        assert len(state["ends"]) == 2
        for prompt_id, body in submitted.items():
            assert comfy.prompts[prompt_id] == body
            assert (
                sum(item["client_id"] == body["client_id"] for item in comfy.prompts.values()) == 1
            )
    assert completed["status"] == "COMPLETED", completed.get("error")
    assert completed["shots"][0]["start_frame_asset_id"] == first_start
    assert completed["shots"][0]["end_frame_asset_id"] != old_end
    assert len(shot_jobs(app, completed, kind="SHOT_START_FRAME")) == 1
    ends = shot_jobs(app, completed, kind="SHOT_END_FRAME")
    assert len(ends) == 2 and sum(CORRECTION in positive(job) for job in ends) == 1
    assert completed["plan"] == reviewed["plan"]
    assert completed["shots"][0]["prompts"] == reviewed["shots"][0]["prompts"]
    assert client.get(f"/api/v1/assets/{old_end}/file").status_code == 200


def test_failed_inherited_start_is_rendered_again_instead_of_reinherited(system):
    client, app, _ = system
    state = install_qa(system, shot_index=1, failed_frame="start_frame", continuous=True)
    reviewed = prepared(system, target_duration=2)
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    first, second = completed["shots"]
    assert second["transition_from_previous"] == "CONTINUE_FRAME"
    assert state["checked_starts"][0] == app.state.assets.path(first["actual_end_frame_asset_id"])
    assert second["start_frame_asset_id"] != first["actual_end_frame_asset_id"]
    starts = shot_jobs(app, completed, 1, "SHOT_START_FRAME")
    assert len(starts) == 1 and CORRECTION in positive(starts[0])
    assert second["start_frame_asset_id"] in starts[0]["output_asset_ids"]
    assert len(shot_jobs(app, completed, 1, "SHOT_END_FRAME")) == 1
    assert len(shot_jobs(app, completed, kind="SHOT_VIDEO")) == 1


def test_qa_failure_on_fixed_user_tail_stops_without_wasting_retries(system):
    client, app, comfy = system
    install_qa(system, failures=None)
    response = client.post(
        "/api/v1/assets", files={"file": ("fixed-end.png", comfy.image_bytes, "image/png")}
    )
    assert response.status_code == 201, response.text
    fixed = response.json()["id"]
    reviewed = prepared(
        system,
        advanced_mode=True,
        workflow_overrides={"default_video": {"end.image": fixed}},
    )
    failed = approve(client, reviewed)
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "QA_FAILED"
    assert len(shot_jobs(app, failed, kind="SHOT_START_FRAME")) == 1
    assert not shot_jobs(app, failed, kind="SHOT_END_FRAME")
    assert not shot_jobs(app, failed, kind="SHOT_VIDEO")
    assert failed["shots"][0]["end_frame_asset_id"] == fixed


def test_qa_correction_cannot_replace_exact_advanced_prompt_override(system):
    client, app, _ = system
    install_qa(system)
    exact = "User-controlled image instruction, kept verbatim."
    reviewed = prepared(
        system,
        advanced_mode=True,
        workflow_overrides={"default_image": {"positive.text": exact}},
    )
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    images = [job for job in shot_jobs(app, completed) if job["type"] != "SHOT_VIDEO"]
    assert len(images) == 3
    assert all(positive(job) == exact for job in images)


def test_unknown_corrected_tail_resumes_same_prompt_without_resubmitting(system):
    client, app, comfy = system
    install_qa(system)
    comfy.settings.render_timeout = 1
    original = comfy.handle
    state = {"offline": True, "ends": [], "job_id": None}

    def handle(request):
        path = request.url.path
        if path.startswith("/history/") and state["ends"]:
            prompt_id = path.rsplit("/", 1)[1]
            if len(state["ends"]) >= 2 and prompt_id == state["ends"][1]:
                state["job_id"] = comfy.prompts[prompt_id]["client_id"]
                if state["offline"]:
                    raise httpx.ReadTimeout("Disconnected during corrected tail", request=request)
        response = original(request)
        if path == "/prompt":
            prompt_id = response.json()["prompt_id"]
            job = app.state.store.get("job", comfy.prompts[prompt_id]["client_id"])
            if job["type"] == "SHOT_END_FRAME":
                state["ends"].append(prompt_id)
        return response

    comfy.handle = handle
    reviewed = prepared(system)
    failed = approve(client, reviewed)
    assert failed["error"]["code"] == "JOB_TIMEOUT", failed.get("error")
    pending = app.state.store.get("job", state["job_id"])
    assert pending["status"] == "UNKNOWN" and CORRECTION in positive(pending)
    submitted = deepcopy(comfy.prompts)
    state["offline"] = False
    response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
    assert response.status_code == 202, response.text
    completed = wait_episode(client, failed["id"])
    assert completed["status"] == "COMPLETED", completed.get("error")
    assert len(state["ends"]) == 2
    assert len(shot_jobs(app, completed, kind="SHOT_START_FRAME")) == 1
    recovered = app.state.store.get("job", pending["id"])
    assert recovered["status"] == "COMPLETED"
    assert recovered["comfy_prompt_id"] == pending["comfy_prompt_id"]
    assert recovered["patched_workflow"] == pending["patched_workflow"]
    for prompt_id, body in submitted.items():
        assert comfy.prompts[prompt_id] == body
        assert sum(item["client_id"] == body["client_id"] for item in comfy.prompts.values()) == 1


def test_cancel_after_corrected_tail_completes_reuses_unbound_completed_job(system, monkeypatch):
    client, app, comfy = system
    install_qa(system)
    generation = app.state.generation
    original = generation.render
    state = {"cancelled": False, "asset_id": None}

    async def interrupt_before_binding(episode, profile, kind, values, assets, key, shot_id=None):
        result = await original(episode, profile, kind, values, assets, key, shot_id)
        if kind == "SHOT_END_FRAME" and CORRECTION in values["prompt"] and not state["cancelled"]:
            state.update(cancelled=True, asset_id=result["id"])
            await generation.cancel(episode["id"])
            raise AppError("CANCELLED", "Cancelled after corrected tail output was saved")
        return result

    monkeypatch.setattr(generation, "render", interrupt_before_binding)
    reviewed = prepared(system)
    cancelled = approve(client, reviewed)
    assert cancelled["status"] == "CANCELLED"
    client.portal.call(generation.queue.join)
    assert cancelled["shots"][0]["end_frame_asset_id"] is None
    starts = shot_jobs(app, cancelled, kind="SHOT_START_FRAME")
    ends = shot_jobs(app, cancelled, kind="SHOT_END_FRAME")
    assert len(starts) == 1 and len(ends) == 2
    pending_binding = next(job for job in ends if state["asset_id"] in job["output_asset_ids"])
    assert pending_binding["status"] == "COMPLETED"
    submitted = deepcopy(comfy.prompts)
    old_images = {
        asset_id: client.get(f"/api/v1/assets/{asset_id}/file").content
        for job in starts + ends
        for asset_id in job["output_asset_ids"]
    }
    response = client.post(f"/api/v1/episodes/{cancelled['id']}/generate")
    assert response.status_code == 202, response.text
    completed = wait_episode(client, cancelled["id"])
    assert completed["status"] == "COMPLETED", completed.get("error")
    assert completed["shots"][0]["end_frame_asset_id"] == state["asset_id"]
    assert (
        completed["shots"][0]["start_frame_asset_id"]
        == cancelled["shots"][0]["start_frame_asset_id"]
    )
    assert len(shot_jobs(app, completed, kind="SHOT_END_FRAME")) == 2
    assert app.state.store.get("job", pending_binding["id"]) == pending_binding
    for prompt_id, body in submitted.items():
        assert comfy.prompts[prompt_id] == body
        assert sum(item["client_id"] == body["client_id"] for item in comfy.prompts.values()) == 1
    for asset_id, content in old_images.items():
        response = client.get(f"/api/v1/assets/{asset_id}/file")
        assert response.status_code == 200 and response.content == content
