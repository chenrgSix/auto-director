"""Real media pipeline with a deliberately blocked visual model."""

import asyncio
import threading
import time
from copy import deepcopy

import pytest

from app.agents.schemas import QAResult
from app.generation.async_reviews import AdvisoryReviews
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_rerun import rerun
from tests.test_qa_policy_pipeline import create, generate, install_qa
from tests.test_video_duration_qa import short_video


def blocked_review(app):
    entered, release, stopped = (threading.Event() for _ in range(3))
    state = {"calls": 0, "active": 0, "maximum": 0}

    class Blocked(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            if schema is not QAResult:
                return await super().generate_json(system, context, schema, images=images)
            state["calls"] += 1
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            try:
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
                return await super().generate_json(system, context, schema, images=images)
            finally:
                state["active"] -= 1
                stopped.set()

    app.state.generation.provider_factory = Blocked
    return entered, release, stopped, state


def settled(client, id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        episode = client.get(f"/api/v1/episodes/{id}").json()
        if all(
            (s.get("visual_review") or {}).get("status") not in {"pending", "running"}
            for s in episode["shots"]
        ):
            return episode
        time.sleep(0.01)
    pytest.fail("Background reviews did not settle")


def test_blocked_model_does_not_block_next_shot_composition_or_next_episode(system):
    client, app, _ = system
    entered, release, _, state = blocked_review(app)
    try:
        result = generate(client, create(client, target_duration=2, max_shot_duration=1))
        assert entered.is_set()
        assert result["status"] == "COMPLETED", result.get("error")
        assert [s["status"] for s in result["shots"]] == ["PASSED", "PASSED"]
        assert [s["visual_review"]["status"] for s in result["shots"]] == ["running", "pending"]
        assert (
            client.get(f"/api/v1/assets/{result['final_video_asset_id']}/file").status_code == 200
        )
        following = generate(client, create(client))
        assert following["status"] == "COMPLETED"
        assert state["calls"] == state["maximum"] == 1
    finally:
        release.set()
    reviewed = settled(client, result["id"])
    settled(client, following["id"])
    assert state["calls"] == 3 and state["maximum"] == 1
    assert reviewed["final_video_asset_id"] == result["final_video_asset_id"]
    assert all(s["visual_review"]["status"] == "completed" for s in reviewed["shots"])
    assert all(len(s["qa"]) == 1 for s in reviewed["shots"])


@pytest.mark.parametrize("policy", ["advisory", "strict"])
def test_disabled_review_makes_no_visual_model_requests_or_qa_samples(system, policy):
    client, app, _ = system
    state = install_qa(system)
    result = generate(client, create(client, qa_enabled=False, qa_policy=policy))
    assert result["status"] == "COMPLETED", result.get("error")
    assert not state["stages"]
    assert not any(
        a["type"] == "QA_SAMPLE_FRAME" for a in app.state.store.list("asset", result["id"])
    )


def test_disabled_review_still_rejects_too_short_video(system, tmp_path):
    client, _, comfy = system
    comfy.video_bytes = short_video(tmp_path).read_bytes()
    result = generate(client, create(client, qa_enabled=False, target_duration=10, max_retries=0))
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "VIDEO_TOO_SHORT"


def test_live_switch_cancels_review_without_interrupting_render_or_changing_story(
    system, monkeypatch
):
    client, app, _ = system
    entered, release, stopped, state = blocked_review(app)
    rendering, proceed = threading.Event(), threading.Event()
    original = app.state.generation.render

    async def pause_second(episode, profile, kind, *args, **kwargs):
        if kind == "SHOT_END_FRAME" and entered.is_set():
            rendering.set()
            assert await asyncio.to_thread(proceed.wait, 10)
        return await original(episode, profile, kind, *args, **kwargs)

    monkeypatch.setattr(app.state.generation, "render", pause_second)
    episode = create(client, target_duration=2, max_shot_duration=1)
    client.post(f"/api/v1/episodes/{episode['id']}/generate")
    try:
        assert entered.wait(5) and rendering.wait(5)
        before = app.state.store.get("episode", episode["id"])
        saved = client.patch(
            f"/api/v1/episodes/{episode['id']}/qa-policy",
            json={
                "expected_version": before["version"],
                "qa_policy": "advisory",
                "qa_enabled": False,
            },
        )
        assert saved.status_code == 200, saved.text
        assert stopped.wait(2) and not saved.json()["qa_enabled"]
        assert saved.json()["status"] == before["status"]
        for key in ("plan", "bible", "references", "continuity"):
            assert saved.json()[key] == before[key]
    finally:
        proceed.set()
        release.set()
    result = wait_episode(client, episode["id"])
    assert result["status"] == "COMPLETED" and state["calls"] == 1
    assert result["shots"][0]["visual_review"]["status"] == "skipped"
    assert not result["shots"][1].get("visual_review")
    assert [s["prompts"] for s in result["shots"]] == [s["prompts"] for s in before["shots"]]
    assert not any(s["qa"] for s in result["shots"])


def test_rerun_discards_old_pending_verdict_and_reviews_only_current_media(system):
    client, app, _ = system
    entered, release, _, _ = blocked_review(app)
    try:
        original = generate(client, create(client))
        assert entered.is_set() and original["status"] == "COMPLETED"
        replacement = rerun(client, original)
        assert replacement["status"] == "COMPLETED"
        assert replacement["shots"][0]["video_asset_id"] != original["shots"][0]["video_asset_id"]
    finally:
        release.set()
    result = settled(client, original["id"])
    assert len(result["shots"][0]["qa"]) == 1
    rows = app.state.store.list("qa", result["id"])
    assert len(rows) == 1
    assert rows[0]["asset_ids"] == [replacement["shots"][0]["video_asset_id"]]
    assert client.get(f"/api/v1/assets/{original['final_video_asset_id']}/file").status_code == 200


@pytest.mark.parametrize("change", ["target", "model", "quality", "disabled"])
def test_late_result_is_rejected_after_review_source_changes(system, change):
    client, app, _ = system
    _, release, _, _ = blocked_review(app)
    try:
        original = generate(client, create(client))

        def mutate(episode):
            if change == "target":
                episode["shots"][0]["prompts"]["video_prompt"] = "A different intended scene"
            elif change == "quality":
                episode["quality"] = "high"
            elif change == "disabled":
                episode["qa_enabled"] = False

        if change == "model":
            app.state.config.vlm_model = "another-review-model"
        else:
            app.state.store.update("episode", original["id"], mutate)
    finally:
        release.set()
    result = settled(client, original["id"])
    assert result["shots"][0]["visual_review"]["status"] == "skipped"
    assert not result["shots"][0]["qa"] and not app.state.store.list("qa", result["id"])
    assert result["final_video_asset_id"] == original["final_video_asset_id"]


def test_pending_review_survives_worker_restart_without_resubmitting_media(system):
    client, app, comfy = system
    _, release, _, state = blocked_review(app)
    result = generate(client, create(client, target_duration=2, max_shot_duration=1))
    submissions = deepcopy(comfy.prompts)
    client.portal.call(app.state.generation.reviews.stop)
    paused = app.state.store.get("episode", result["id"])
    assert all(s["visual_review"]["status"] == "pending" for s in paused["shots"])
    release.set()
    app.state.generation.reviews = AdvisoryReviews(app.state.generation)
    client.portal.call(app.state.generation.reviews.start)
    restored = settled(client, result["id"])
    assert all(s["visual_review"]["status"] == "completed" for s in restored["shots"])
    assert state["calls"] == 3  # Interrupted model request plus one per pending shot.
    assert comfy.prompts == submissions
    assert restored["final_video_asset_id"] == result["final_video_asset_id"]


def test_delete_completed_episode_cancels_background_review_without_orphan_results(system):
    client, app, _ = system
    _, release, stopped, _ = blocked_review(app)
    result = generate(client, create(client))
    try:
        response = client.delete(f"/api/v1/episodes/{result['id']}?delete_assets=true")
        assert response.status_code == 204, response.text
        assert stopped.wait(2)
    finally:
        release.set()
    assert client.get(f"/api/v1/episodes/{result['id']}").status_code == 404
    assert not app.state.store.list("qa", result["id"])
    assert not app.state.store.list("asset", result["id"])
    assert not app.state.generation.reviews.worker.done()


def test_background_timeout_is_visible_without_failing_completed_film(system):
    client, app, _ = system
    app.state.config.llm_timeout = 1
    _, release, _, _ = blocked_review(app)
    try:
        original = generate(client, create(client))
        assert original["status"] == "COMPLETED"
        result = settled(client, original["id"])
        shot = result["shots"][0]
        assert shot["visual_review"]["status"] == "failed"
        assert shot["visual_review"]["error"]["code"] == "LLM_TIMEOUT"
        assert shot["needs_review"] and result["status"] == "COMPLETED" and result["error"] is None
    finally:
        release.set()
