from copy import deepcopy

import pytest

from app.core.errors import AppError
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_video_graph


def complete(system, **extra):
    client, _, _ = system
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Rerun fixture",
            "target_duration": 1,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode.get("error")
    return episode


def rerun(client, episode, **extra):
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/rerun",
        json={
            "expected_version": episode["version"],
            "scope": "video",
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    result = wait_episode(client, episode["id"])
    assert result["status"] == "COMPLETED", result.get("error")
    return result


@pytest.mark.parametrize("new_seed", [True, False])
def test_video_rerun_preserves_story_frames_and_downloadable_history(system, new_seed):
    client, app, comfy = system
    old = complete(system)
    job = next(j for j in app.state.store.list("job", old["id"]) if j["type"] == "SHOT_VIDEO")
    submissions = len(comfy.prompts)
    result = rerun(client, old, new_seed=new_seed)
    for key in ["plan", "bible", "references", "seed"]:
        assert result[key] == old[key]
    a, b = old["shots"][0], result["shots"][0]
    for key in ["id", "prompts", "start_frame_asset_id", "end_frame_asset_id", "duration"]:
        assert b[key] == a[key]
    assert b["video_asset_id"] != a["video_asset_id"]
    assert len(comfy.prompts) == submissions + 1
    new_job = next(j for j in app.state.store.list("job", old["id"]) if j["type"] == "SHOT_VIDEO")
    assert (new_job["input_values"]["seed"] != job["input_values"]["seed"]) == new_seed
    assert result["rerun_history"][0]["shots"] == old["shots"]
    for asset in [old["final_video_asset_id"], a["video_asset_id"], a["start_frame_asset_id"]]:
        assert client.get(f"/api/v1/assets/{asset}/file").status_code == 200


@pytest.mark.parametrize("scope", ["video", "keyframes", "prompts"])
def test_i2v_rerun_never_requires_or_generates_end_frame(system, scope):
    client, app, comfy = system
    graph = image_to_video_graph(client.get("/api/v1/workflows/default_video").json()["workflow"])
    profile = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "Rerun I2V",
            "capability": "IMAGE_TO_VIDEO",
            "workflow": graph,
        },
    ).json()
    old = complete(system, video_workflow_id=profile["id"])
    count = len(comfy.prompts)
    result = rerun(client, old, scope=scope)
    assert len(comfy.prompts) == count + (1 if scope == "video" else 2)
    assert result["shots"][0]["end_frame_asset_id"] is None
    assert not any(j["type"] == "SHOT_END_FRAME" for j in app.state.store.list("job", old["id"]))


def test_selected_rerun_updates_only_continuity_chain(system):
    client, app, _ = system
    old = complete(system, target_duration=3, max_shot_duration=1)

    # Stop the dependency chain at the third shot; retain a real completed result there.
    def cut(episode):
        episode["shots"][2]["transition_from_previous"] = "CUT"

    old = app.state.store.update("episode", old["id"], cut)
    result = rerun(client, old, shot_ids=[old["shots"][0]["id"]])
    assert result["shots"][2] == old["shots"][2]
    assert result["shots"][0]["start_frame_asset_id"] == old["shots"][0]["start_frame_asset_id"]
    assert (
        result["shots"][1]["start_frame_asset_id"]
        == result["shots"][0]["actual_end_frame_asset_id"]
    )
    assert result["shots"][1]["end_frame_asset_id"] != old["shots"][1]["end_frame_asset_id"]
    assert result["rerun_history"][0]["affected_shot_ids"] == [s["id"] for s in old["shots"][:2]]


def test_all_keyframes_rerun_preserves_reference_assets(system):
    client, _, _ = system
    old = complete(system, target_duration=2, max_shot_duration=1)
    result = rerun(client, old, scope="keyframes")
    assert result["plan"] == old["plan"]
    assert result["references"] == old["references"]
    assert len(result["rerun_history"][0]["affected_shot_ids"]) == 2
    for a, b in zip(old["shots"], result["shots"], strict=True):
        assert a["prompts"] == b["prompts"]
        assert a["video_asset_id"] != b["video_asset_id"]
        assert a["start_frame_asset_id"] != b["start_frame_asset_id"]


def test_repeated_whole_film_rerun_keeps_all_old_videos_after_failure(system, monkeypatch):
    client, _, _ = system
    original = complete(system, target_duration=2, max_shot_duration=1)
    second = rerun(client, original)
    assert second["final_video_asset_id"] != original["final_video_asset_id"]
    assert second["rerun_history"][0]["shots"] == original["shots"]
    for before, after in zip(original["shots"], second["shots"], strict=True):
        assert before["video_asset_id"] != after["video_asset_id"]
    old_ids = {
        asset
        for episode in (original, second)
        for asset in [
            episode["final_video_asset_id"],
            *[s["video_asset_id"] for s in episode["shots"]],
        ]
    }
    old_files = {id: client.get(f"/api/v1/assets/{id}/file").content for id in old_ids}

    async def fail_composition(*args, **kwargs):
        raise AppError("FFMPEG_FAILED", "Synthetic composition failure")

    monkeypatch.setattr("app.generation.pipeline.compose", fail_composition)
    current = second
    # A second failure must not hide successful versions behind an empty final-video snapshot.
    for attempt in range(2):
        response = client.post(
            f"/api/v1/episodes/{current['id']}/rerun",
            json={"expected_version": current["version"], "scope": "video"},
        )
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued["rerun_history"][1]["final_video_asset_id"] == second["final_video_asset_id"]
        current = wait_episode(client, current["id"])
        assert current["status"] == "FAILED"
        assert current["error"]["code"] == "FFMPEG_FAILED"
        assert current["final_video_asset_id"] is None
        assert len(current["rerun_history"]) == attempt + 2
        assert current["rerun_history"][0] == second["rerun_history"][0]
        assert current["rerun_history"][1]["shots"] == second["shots"]
        for key in ["plan", "bible", "references"]:
            assert current[key] == original[key]
        for id, content in old_files.items():
            download = client.get(f"/api/v1/assets/{id}/file?download=true")
            assert download.status_code == 200
            assert download.content == content
            assert "attachment" in download.headers["content-disposition"]
    assert current["rerun_history"][-1]["final_video_asset_id"] is None


def test_rerun_rejects_invalid_or_stale_selection_without_changes(system):
    client, app, comfy = system
    old = complete(system)
    count = len(comfy.prompts)
    for extra, status in [
        ({"expected_version": old["version"] - 1}, 409),
        ({"shot_ids": []}, 422),
        ({"shot_ids": ["other-episode-shot"]}, 409),
        ({"shot_ids": [old["shots"][0]["id"]] * 2}, 409),
        ({"scope": "everything"}, 422),
    ]:
        response = client.post(
            f"/api/v1/episodes/{old['id']}/rerun",
            json={
                "expected_version": old["version"],
                "scope": "video",
                **extra,
            },
        )
        assert response.status_code == status, response.text
        assert app.state.store.get("episode", old["id"]) == old
    assert len(comfy.prompts) == count


@pytest.mark.parametrize("scope", ["keyframes", "prompts"])
def test_unresolved_job_blocks_new_and_legacy_rerun_without_erasing_results(system, scope):
    client, app, comfy = system
    old = complete(system)
    job = next(j for j in app.state.store.list("job", old["id"]) if j["type"] == "SHOT_VIDEO")
    app.state.store.update("job", job["id"], {"status": "UNKNOWN"})
    count = len(comfy.prompts)
    response = client.post(
        f"/api/v1/episodes/{old['id']}/rerun",
        json={
            "expected_version": old["version"],
            "scope": scope,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UNRESOLVED_JOB"
    for path in ["retry-video", "retry-keyframes"]:
        assert client.post(f"/api/v1/shots/{old['shots'][0]['id']}/{path}").status_code == 409
    assert (
        client.patch(
            f"/api/v1/episodes/{old['id']}/timeline",
            json={"shots": [{"id": s["id"], "enabled": True} for s in old["shots"]]},
        ).status_code
        == 409
    )
    assert app.state.store.get("episode", old["id"]) == old
    assert len(comfy.prompts) == count


def test_rerun_seed_respects_explicit_user_override(system):
    client, app, _ = system
    profile = client.get("/api/v1/workflows/default_video").json()
    binding = profile["bindings"]["seed"]
    key = f"{binding['node_id']}.{binding['input']}"
    old = complete(system, advanced_mode=True, workflow_overrides={"default_video": {key: 777}})
    result = rerun(client, old)
    assert result["shots"][0]["seed_offset"] == 10000
    job = next(j for j in app.state.store.list("job", old["id"]) if j["type"] == "SHOT_VIDEO")
    assert job["patched_workflow"][binding["node_id"]]["inputs"][binding["input"]] == 777
    assert job["parameter_sources"][key] == "user"


def test_active_and_unapproved_rerun_are_atomic(system):
    client, app, _ = system
    old = complete(system)
    for changes, code in [
        ({"status": "RENDERING_VIDEO"}, "CONFLICT"),
        (
            {"status": "AWAITING_REVIEW", "preview_required": True, "preview_approved_at": None},
            "PREVIEW_REQUIRED",
        ),
    ]:
        current = app.state.store.update("episode", old["id"], changes)
        response = client.post(
            f"/api/v1/episodes/{old['id']}/rerun",
            json={
                "expected_version": current["version"],
                "scope": "video",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert app.state.store.get("episode", old["id"]) == current


@pytest.mark.parametrize("scope", ["video", "prompts"])
def test_rerun_skips_disabled_shots_and_tracks_dependencies_across_them(system, scope):
    client, app, _ = system
    old = complete(system, target_duration=3, max_shot_duration=1)

    def disable(episode):
        episode["shots"][1]["enabled"] = False

    current = app.state.store.update("episode", old["id"], disable)
    disabled = deepcopy(current["shots"][1])
    result = rerun(client, current, shot_ids=[current["shots"][0]["id"]], scope=scope)
    assert result["shots"][1] == disabled
    assert (
        result["shots"][2]["start_frame_asset_id"]
        == result["shots"][0]["actual_end_frame_asset_id"]
    )
    assert len(result["rerun_history"][0]["affected_shot_ids"]) == 2


def test_recompose_keeps_complete_sources_and_archives_previous_film(system):
    from tests.media_assertions import assert_full_duration

    client, app, comfy = system
    old = complete(system, target_duration=5, max_shot_duration=3)
    before = len(comfy.prompts)
    old_file = client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").content
    for _ in range(2):
        response = client.post(f"/api/v1/episodes/{old['id']}/compose")
        assert response.status_code == 202
        final = wait_episode(client, old["id"])
        assert final["status"] == "COMPLETED", final.get("error")
        assert_full_duration(app, final)
        assert final["final_duration"] > final["target_duration"]
        for key in ("shots", "plan", "bible", "references"):
            assert final[key] == old[key]
        history = final["rerun_history"][-1]
        assert history["scope"] == "compose"
        assert history["final_video_asset_id"] == old["final_video_asset_id"]
        assert history["shots"] == old["shots"]
        assert final["final_video_asset_id"] != old["final_video_asset_id"]
        assert client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").content == old_file
        assert len(comfy.prompts) == before
        old = final
        old_file = client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").content


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_recompose_leaves_current_film_available(
    system, monkeypatch, cancelled
):
    client, app, comfy = system
    old = complete(system)
    calls = len(comfy.prompts)
    original_file = client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").content

    async def interrupted(*args, **kwargs):
        if cancelled:
            await app.state.generation.cancel(old["id"])
        raise AppError("COMPOSE_FAILED", "Synthetic interruption")

    monkeypatch.setattr("app.generation.pipeline.compose", interrupted)
    assert client.post(f"/api/v1/episodes/{old['id']}/compose").status_code == 202
    final = wait_episode(client, old["id"])
    assert final["status"] == ("CANCELLED" if cancelled else "FAILED")
    for field in ("final_video_asset_id", "final_duration", "shots", "plan", "bible"):
        assert final[field] == old[field]
    assert final.get("rerun_history", []) == old.get("rerun_history", [])
    assert client.get(f"/api/v1/assets/{old['final_video_asset_id']}/file").content == original_file
    assert len(comfy.prompts) == calls


def test_legacy_continuity_tail_refresh_uses_full_video_without_rerender(system, monkeypatch):
    import asyncio

    from app.generation import pipeline

    client, app, comfy = system
    old = complete(system)
    shot = old["shots"][0]
    tail = shot["actual_end_frame_asset_id"]
    shot.pop("full_tail_video_asset_id")
    app.state.store.update("episode", old["id"], {"shots": [shot]})
    original = pipeline.extract_frame
    calls = []

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_frame", capture)
    submissions = len(comfy.prompts)
    fresh = asyncio.run(app.state.generation.ensure_full_tail(old["id"], shot))
    assert fresh["actual_end_frame_asset_id"] != tail
    assert fresh["full_tail_video_asset_id"] == shot["video_asset_id"]
    assert fresh["video_asset_id"] == shot["video_asset_id"]
    assert calls[0][1] == {} and len(calls[0][0]) == 2
    assert asyncio.run(app.state.generation.ensure_full_tail(old["id"], fresh)) == fresh
    assert len(calls) == 1 and len(comfy.prompts) == submissions
    assert client.get(f"/api/v1/assets/{tail}/file").status_code == 200
