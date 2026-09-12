"""Advisory QA finishes media while keeping semantic concerns visible and recoverable."""

import asyncio
import threading
from collections import Counter
from copy import deepcopy

import httpx
import pytest

from app.agents.schemas import QAResult
from app.core.config import Settings
from app.core.errors import AppError
from app.generation.qa_review import video_review_key
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_cancellation import wait_idle
from tests.test_episode_rerun import rerun
from tests.test_keyframe_targets import clone_ends
from tests.test_video_duration_qa import short_video


def install_qa(system, *, error=None, failing=True):
    _, app, _ = system
    state = {"stages": [], "failing": failing, "error": error}

    class ReviewProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if schema is QAResult:
                state["stages"].append(context["stage"])
                if state["error"]:
                    raise AppError(state["error"], "Visual review service failed")
            result = await super().generate_json(system, context, schema, images=images)
            if schema is QAResult and state["failing"]:
                result.character_consistency = 0.2
                result.action_accuracy = 0.2
                result.explanation = "The rendered subject and movement need human review."
            return result

    app.state.generation.provider_factory = ReviewProvider
    return state


def create(client, **extra):
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Finish the reviewed film without repeatedly discarding generated footage",
            "target_duration": 1,
            "qa_policy": "advisory",
            "qa_enabled": True,
            "width": 256,
            "height": 256,
            "max_retries": 2,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def generate(client, episode):
    assert client.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 202
    return wait_episode(client, episode["id"])


@pytest.mark.parametrize("quality", ["standard", "high"])
def test_advisory_finishes_multiple_shots_with_one_render_each_and_visible_review(system, quality):
    client, app, _ = system
    state = install_qa(system)
    result = generate(
        client, create(client, target_duration=6, max_shot_duration=3, quality=quality)
    )
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["error"] is None and result["final_duration"] == pytest.approx(6, abs=0.1)
    assert len(result["shots"]) == 2
    assert state["stages"] == ["video", "video"]
    jobs = app.state.store.list("job", result["id"])
    kinds = Counter(job["type"] for job in jobs)
    assert kinds["SHOT_START_FRAME"] == 1  # The second shot inherits the actual preceding end.
    assert kinds["SHOT_END_FRAME"] == kinds["SHOT_VIDEO"] == 2
    assert all(job["status"] == "COMPLETED" for job in jobs)
    for shot in result["shots"]:
        assert shot["status"] == "PASSED" and shot["video_asset_id"]
        assert shot["actual_end_frame_asset_id"] and shot["needs_review"]
        assert any(note["code"] == "QA_FAILED" for note in shot["review_notes"])
        assert len(shot["qa"]) == 1 and shot["qa"][0]["disposition"] == "warning"
        assert shot["qa"][0]["retry_scope"]
        assert not shot.get("qa_retry") and not shot.get("qa_frame_corrections")
        video_job = next(
            job for job in jobs if job["shot_id"] == shot["id"] and job["type"] == "SHOT_VIDEO"
        )
        assert shot["video_asset_id"] in video_job["output_asset_ids"]
        assert client.get(f"/api/v1/assets/{shot['video_asset_id']}/file").status_code == 200
    assert client.get(f"/api/v1/assets/{result['final_video_asset_id']}/file").status_code == 200


@pytest.mark.parametrize("code", ["LLM_TIMEOUT", "LLM_INVALID_OUTPUT", "CONFIGURATION_REQUIRED"])
def test_advisory_model_failure_marks_review_and_continues_later_shots(system, code):
    client, app, _ = system
    state = install_qa(system, error=code)
    result = generate(
        client, create(client, target_duration=2, max_shot_duration=1, quality="high")
    )
    assert result["status"] == "COMPLETED", result.get("error")
    assert state["stages"] == ["video", "video"]
    assert all(shot["status"] == "PASSED" and shot["needs_review"] for shot in result["shots"])
    assert all(
        any(note["code"] == code for note in shot["review_notes"]) for shot in result["shots"]
    )
    jobs = app.state.store.list("job", result["id"])
    assert sum(job["type"] == "SHOT_START_FRAME" for job in jobs) == 1
    assert sum(job["type"] == "SHOT_VIDEO" for job in jobs) == 2


def test_advisory_without_configured_vlm_finishes_but_never_claims_visual_acceptance(system):
    client, app, _ = system
    app.state.config.vlm_model = ""
    state = install_qa(system, failing=False)
    result = generate(client, create(client))
    assert result["status"] == "COMPLETED", result.get("error")
    assert state["stages"] == []
    assert result["shots"][0]["needs_review"] and result["shots"][0]["review_notes"]
    assert result["shots"][0]["video_asset_id"]


def test_advisory_duplicate_endpoints_warn_without_repainting_or_stopping_video(system):
    client, app, _ = system
    clone_ends(system, None)
    state = install_qa(system, failing=False)
    result = generate(client, create(client))
    assert result["status"] == "COMPLETED", result.get("error")
    shot = result["shots"][0]
    assert shot["keyframe_comparison"]["near_duplicate"]
    assert shot["needs_review"]
    assert any(note["code"] == "KEYFRAMES_TOO_SIMILAR" for note in shot["review_notes"])
    assert state["stages"] == ["video"]
    kinds = Counter(job["type"] for job in app.state.store.list("job", result["id"]))
    assert kinds["SHOT_START_FRAME"] == kinds["SHOT_END_FRAME"] == kinds["SHOT_VIDEO"] == 1


def test_explicit_strict_policy_still_stops_semantic_failure_before_video(system):
    client, app, _ = system
    state = install_qa(system)
    result = generate(client, create(client, qa_policy="strict", max_retries=0))
    assert result["status"] == "FAILED" and result["error"]["code"] == "QA_FAILED"
    assert state["stages"] == ["keyframes"]
    assert not any(job["type"] == "SHOT_VIDEO" for job in app.state.store.list("job", result["id"]))


def test_advisory_short_video_still_fails_technical_validation_before_later_shots(system, tmp_path):
    client, app, comfy = system
    comfy.video_bytes = short_video(tmp_path).read_bytes()
    state = install_qa(system, failing=False)
    result = generate(client, create(client, target_duration=10, max_retries=0))
    assert result["status"] == "FAILED" and result["error"]["code"] == "VIDEO_TOO_SHORT"
    assert result["shots"][0]["status"] == "FAILED"
    assert result["shots"][1]["status"] == "PENDING"
    assert not result["final_video_asset_id"] and not state["stages"]
    videos = [
        job for job in app.state.store.list("job", result["id"]) if job["type"] == "SHOT_VIDEO"
    ]
    assert len(videos) == 1 and videos[0]["status"] == "FAILED"
    assert videos[0]["output_asset_ids"]  # Keep the rejected short output in history.


def test_advisory_does_not_swallow_media_extraction_failure(system, monkeypatch):
    client, _, _ = system
    state = install_qa(system)

    async def corrupt_media(*args, **kwargs):
        raise AppError("INVALID_MEDIA", "The rendered video cannot be decoded")

    monkeypatch.setattr("app.generation.pipeline.extract_frame", corrupt_media)
    result = generate(client, create(client, max_retries=0))
    assert result["status"] == "FAILED" and result["error"]["code"] == "INVALID_MEDIA"
    assert not result["final_video_asset_id"] and not state["stages"]
    assert result["shots"][0]["video_asset_id"]  # Technical failure does not erase history.


def test_advisory_unknown_submission_recovers_original_video_without_duplicate_render(system):
    client, app, comfy = system
    state = install_qa(system)
    original = comfy.handle
    offline = {"value": True}
    comfy.settings.render_timeout = 1

    def interrupted_video(request):
        if request.url.path.startswith("/history/"):
            prompt = comfy.prompts.get(request.url.path.rsplit("/", 1)[1])
            if (
                prompt
                and prompt["prompt"]["save"]["class_type"] == "SaveVideo"
                and offline["value"]
            ):
                raise httpx.ReadTimeout("temporary video history disconnect", request=request)
        return original(request)

    comfy.handle = interrupted_video
    failed = generate(client, create(client, max_retries=0))
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "JOB_TIMEOUT"
    jobs = app.state.store.list("job", failed["id"])
    pending = next(job for job in jobs if job["type"] == "SHOT_VIDEO")
    assert pending["status"] == "UNKNOWN" and pending["comfy_prompt_id"]
    assert state["stages"] == [] and not failed["final_video_asset_id"]
    submissions = deepcopy(comfy.prompts)
    offline["value"] = False
    result = generate(client, failed)
    assert result["status"] == "COMPLETED", result.get("error")
    assert comfy.prompts == submissions and state["stages"] == ["video"]
    recovered = app.state.store.get("job", pending["id"])
    assert recovered["status"] == "COMPLETED"
    assert result["shots"][0]["video_asset_id"] in recovered["output_asset_ids"]


def test_cancelled_advisory_model_wait_stays_cancelled_and_keeps_the_rendered_video(system):
    client, app, _ = system
    entered = threading.Event()

    class BlockedReview(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if schema is QAResult:
                assert context["stage"] == "video"
                entered.set()
                await asyncio.Event().wait()
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = BlockedReview
    created = create(client)
    assert client.post(f"/api/v1/episodes/{created['id']}/generate").status_code == 202
    try:
        assert entered.wait(5)
        before = app.state.store.get("episode", created["id"])
        assert before["shots"][0]["video_asset_id"]
    finally:
        assert client.post(f"/api/v1/episodes/{created['id']}/cancel").status_code == 200
    wait_idle(app.state.generation, created["id"])
    after = app.state.store.get("episode", created["id"])
    assert after["status"] == "CANCELLED" and not after["final_video_asset_id"]
    assert after["shots"][0]["video_asset_id"] == before["shots"][0]["video_asset_id"]
    assert not after["shots"][0].get("qa_video_check")


def test_cancel_after_completed_review_resumes_cached_video_check_and_rerun_checks_new_media(
    system, monkeypatch
):
    import app.generation.pipeline as pipeline

    client, app, comfy = system
    state = install_qa(system)
    entered, release = threading.Event(), threading.Event()
    original = pipeline.extract_frame

    async def block_after_qa(*args, **kwargs):
        if state["stages"] and len(args) == 2:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            app.state.generation.check_cancel(created["id"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_frame", block_after_qa)
    created = create(client)
    assert client.post(f"/api/v1/episodes/{created['id']}/generate").status_code == 202
    try:
        assert entered.wait(5)
    finally:
        assert client.post(f"/api/v1/episodes/{created['id']}/cancel").status_code == 200
        release.set()
    wait_idle(app.state.generation, created["id"])
    cancelled = app.state.store.get("episode", created["id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["shots"][0].get("qa_video_check")
    assert state["stages"] == ["video"]
    submissions = deepcopy(comfy.prompts)
    monkeypatch.setattr(pipeline, "extract_frame", original)
    resumed = generate(client, cancelled)
    assert resumed["status"] == "COMPLETED", resumed.get("error")
    assert state["stages"] == ["video"] and comfy.prompts == submissions
    assert resumed["shots"][0]["video_asset_id"] == cancelled["shots"][0]["video_asset_id"]
    assert resumed["shots"][0]["needs_review"]
    assert len(resumed["shots"][0]["qa"]) == 1

    state["failing"] = False
    retried = rerun(client, resumed)
    assert state["stages"] == ["video", "video"]
    assert retried["shots"][0]["video_asset_id"] != resumed["shots"][0]["video_asset_id"]
    assert not retried["shots"][0].get("needs_review") and not retried["shots"][0].get(
        "review_notes"
    )
    assert retried["shots"][0]["qa_video_check"] != resumed["shots"][0]["qa_video_check"]
    assert retried["rerun_history"][-1]["shots"] == resumed["shots"]
    assert client.get(f"/api/v1/assets/{resumed['final_video_asset_id']}/file").status_code == 200


@pytest.mark.parametrize(
    "changed",
    ["video", "previous_end", "duration", "target", "quality", "policy", "model", "endpoint"],
)
def test_review_cache_invalidates_when_media_review_target_or_model_contract_changes(changed):
    episode = {"quality": "standard", "qa_policy": "advisory"}
    shot = {
        "video_asset_id": "video-a",
        "duration": 3,
        "action": "A chick emerges from its shell",
        "prompts": {"video_prompt": "A chick emerges from its shell in the straw nest"},
    }
    previous = {"actual_end_frame_asset_id": "previous-a"}
    settings = Settings(_env_file=None, vlm_model="vision-a", llm_base_url="http://fixture-a/v1")
    original = video_review_key(episode, shot, previous, settings)
    if changed == "video":
        shot["video_asset_id"] = "video-b"
    elif changed == "previous_end":
        previous["actual_end_frame_asset_id"] = "previous-b"
    elif changed == "duration":
        shot["duration"] = 4
    elif changed == "target":
        shot["prompts"]["video_prompt"] = "The chick returns to its shell"
    elif changed == "quality":
        episode["quality"] = "high"
    elif changed == "policy":
        episode["qa_policy"] = "strict"
    elif changed == "model":
        settings.vlm_model = "vision-b"
    elif changed == "endpoint":
        settings.llm_base_url = "http://fixture-b/v1"
    assert video_review_key(episode, shot, previous, settings) != original


def test_review_cache_ignores_status_and_historical_feedback_updates():
    episode = {"quality": "standard", "qa_policy": "advisory"}
    shot = {"video_asset_id": "video", "action": "A chick emerges from its shell"}
    settings = Settings(_env_file=None, vlm_model="vision")
    original = video_review_key(episode, shot, None, settings)
    shot.update(
        status="QA",
        qa=[{"stage": "video", "explanation": "Prior verdict"}],
        needs_review=True,
        review_notes=[{"message": "Prior review note"}],
        qa_video_check={"key": original},
    )
    episode.update(status="CANCELLED", version=50)
    assert video_review_key(episode, shot, None, settings) == original


@pytest.mark.parametrize("changed", ["quality", "model", "strict_policy"])
def test_rechecking_same_video_replaces_old_warning_and_archives_review_without_rendering(
    system, monkeypatch, changed
):
    import app.generation.pipeline as pipeline

    client, app, comfy = system
    state = install_qa(system)
    entered, release = threading.Event(), threading.Event()
    original = pipeline.extract_frame

    async def pause_after_review(*args, **kwargs):
        if state["stages"] and len(args) == 2:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            app.state.generation.check_cancel(created["id"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_frame", pause_after_review)
    created = create(client, quality="high")
    assert client.post(f"/api/v1/episodes/{created['id']}/generate").status_code == 202
    try:
        assert entered.wait(5)
    finally:
        assert client.post(f"/api/v1/episodes/{created['id']}/cancel").status_code == 200
        release.set()
    wait_idle(app.state.generation, created["id"])
    cancelled = app.state.store.get("episode", created["id"])
    old_shot = cancelled["shots"][0]
    assert old_shot["needs_review"] and len(old_shot["qa"]) == 1
    assert old_shot["review_notes"] and old_shot["qa_video_check"]
    submissions = deepcopy(comfy.prompts)
    old_jobs = app.state.store.list("job", created["id"])

    if changed == "model":
        app.state.config.vlm_model = "updated-vision-model"
    else:
        path, field, value = (
            ("quality", "quality", "standard")
            if changed == "quality"
            else ("qa-policy", "qa_policy", "strict")
        )
        saved = client.patch(
            f"/api/v1/episodes/{created['id']}/{path}",
            json={"expected_version": cancelled["version"], field: value},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["shots"][0]["video_asset_id"] == old_shot["video_asset_id"]
    state["failing"] = False
    monkeypatch.setattr(pipeline, "extract_frame", original)
    result = generate(client, cancelled)
    assert result["status"] == "COMPLETED", result.get("error")
    shot = result["shots"][0]
    assert shot["video_asset_id"] == old_shot["video_asset_id"]
    assert not shot["needs_review"] and shot["review_notes"] == []
    assert shot["qa"][:1] == old_shot["qa"]
    assert len(shot["qa"]) == 2 and shot["qa"][-1]["retry_scope"] is None
    assert shot["review_history"][-1]["notes"] == old_shot["review_notes"]
    assert state["stages"] == ["video", "video"]
    assert comfy.prompts == submissions
    assert app.state.store.list("job", created["id"]) == old_jobs
