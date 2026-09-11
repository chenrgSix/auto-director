import pytest

from tests.test_ai_parameters import CreativeProvider, configure_ai_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview, wait_preview
from tests.test_episode_workflows import imported, rebind
from tests.test_workflow_duration import install_cloud


def assert_story(before, after):
    assert after["plan"] == before["plan"] and after["title"] == before["title"]
    assert after["metrics"] == before["metrics"]
    assert len(after["shots"]) == len(before["shots"])
    for old, new in zip(before["shots"], after["shots"], strict=True):
        assert {k: v for k, v in old.items() if k not in {"prompts", "preview_prompt_view"}} == {
            k: v for k, v in new.items() if k not in {"prompts", "preview_prompt_view"}
        }
        if old.get("prompts"):
            assert {k: v for k, v in old["prompts"].items() if k != "ai_parameters"} == {
                k: v for k, v in new["prompts"].items() if k != "ai_parameters"
            }
        else:
            assert new["prompts"] is None
    assert {k: v for k, v in (before.get("bible") or {}).items() if k != "ai_parameters"} == {
        k: v for k, v in (after.get("bible") or {}).items() if k != "ai_parameters"
    }


def test_90_second_i2i_rebind_preserves_edited_story_without_external_calls(system, monkeypatch):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    app.state.generation.provider_factory = CreativeProvider
    episode = preview(system, target_duration=90, max_shot_duration=5)
    body = edits(episode)
    body["shots"][0]["start_frame_prompt"] = "Reviewed opening image"
    body["shots"][3].update(
        title="Reviewed vortex",
        video_prompt="Three tigers enter the vortex",
        end_frame_prompt="Three tigers tumble deeper",
    )
    edited = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=body)
    assert edited.status_code == 200, edited.text
    episode = edited.json()
    replacement = imported(app, "IMAGE_TO_IMAGE")
    old_calls = list(comfy.calls)

    def forbidden(*args, **kwargs):
        raise AssertionError("Changing a workflow must not regenerate the story or submit media")

    monkeypatch.setattr(app.state.generation, "provider_factory", forbidden)
    monkeypatch.setattr(app.state.engine, "client", forbidden)
    result = rebind(client, episode, image_workflow_id=replacement["id"])
    assert result.status_code == 200, result.text
    saved = result.json()
    assert_story(episode, saved)
    assert saved["status"] == "AWAITING_REVIEW" and not saved["preview_approved_at"]
    assert len(saved["shots"]) == 18 and sum(s["duration"] for s in saved["shots"]) == 90
    assert saved["bible"] == episode["bible"]  # Same text-to-image reference producer.
    for shot in saved["shots"]:
        assert "default_image" not in shot["prompts"]["ai_parameters"]
        assert shot["prompts"]["ai_parameters"]["default_video"]["sampler.denoise"] == 0.6
        assert replacement["id"] not in shot["prompts"]["ai_parameters"]
    assert saved["workflow_binding_history"][-1]["previous_state"]["shots"] == episode["shots"]
    assert saved["preview"]["workflow_versions"][replacement["id"]] == replacement["version"]
    assert client.get(f"/api/v1/episodes/{episode['id']}").json() == saved
    assert not comfy.prompts and comfy.calls == old_calls
    assert client.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 409
    assert rebind(client, episode, image_workflow_id=replacement["id"]).status_code == 409
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/approve",
            json={"expected_version": episode["version"]},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("capability", ["IMAGE_TO_IMAGE", "IMAGE_TO_VIDEO"])
def test_rebound_preview_approval_reuses_prompts_and_binds_real_assets(system, capability):
    client, app, comfy = system
    comfy.info["VAEEncode"] = {
        "input": {"required": {"pixels": ["IMAGE", {}], "vae": ["VAE", {}]}},
        "output": ["LATENT"],
    }
    episode = preview(system, target_duration=1)
    replacement = imported(app, capability)
    key = "image_workflow_id" if capability == "IMAGE_TO_IMAGE" else "video_workflow_id"
    changed = rebind(client, episode, **{key: replacement["id"]})
    assert changed.status_code == 200, changed.text
    saved = changed.json()
    assert_story(episode, saved)
    if capability == "IMAGE_TO_VIDEO":
        assert saved["preview"]["capability"] == capability
        assert "end_frame_prompt" in saved["shots"][0]["preview_prompt_view"]["locked"]
    else:
        saved = rebind(client, saved, reference_workflow_id=imported(app)["id"]).json()
        assert_story(episode, saved)
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/approve", json={"expected_version": saved["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final["error"]
    assert final["plan"] == episode["plan"]
    assert final["metrics"]["llm_calls"] == episode["metrics"]["llm_calls"]
    jobs = app.state.store.list("job", episode["id"])
    if capability == "IMAGE_TO_VIDEO":
        assert not any(job["type"] == "SHOT_END_FRAME" for job in jobs)
    else:
        frames = [j for j in jobs if j["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"}]
        assert len(frames) == 2
        for job in frames:
            assert job["workflow_id"] == replacement["id"]
            assert job["asset_bindings"]["reference_image"]
            assert "placeholder" not in job["patched_workflow"]["reference"]["inputs"]["image"]


@pytest.mark.parametrize("conflict", ["short_video", "invalid_ai", "missing_end_prompt"])
def test_incompatible_rebind_preserves_entire_episode_atomically(system, conflict):
    client, app, comfy = system
    episode = preview(system, target_duration=5, max_shot_duration=5)
    if conflict == "invalid_ai":
        configure_ai_parameters(client, comfy)
        client.post("/api/v1/workflows/default_video/validate")

        def invalid(record):
            record["shots"][0]["prompts"]["ai_parameters"] = {
                "default_video": {"sampler.denoise": 10}
            }

        episode = app.state.store.update("episode", episode["id"], invalid)
        selection = {"image_workflow_id": imported(app)["id"]}
    elif conflict == "missing_end_prompt":

        def clear(record):
            record["shots"][0]["prompts"]["end_frame_prompt"] = ""

        episode = app.state.store.update("episode", episode["id"], clear)
        selection = {"video_workflow_id": imported(app, "FIRST_LAST_TO_VIDEO")["id"]}
    else:
        video = imported(app, "IMAGE_TO_VIDEO")
        app.state.store.update(
            "workflow", video["id"], {"capabilities": {**video["capabilities"], "max_duration": 2}}
        )
        selection = {"video_workflow_id": video["id"]}
    result = rebind(client, episode, **selection)
    assert result.status_code == 400, result.text
    if conflict == "short_video":
        assert "第 1 镜" in result.json()["error"]["message"]
        assert "原故事与工作流未改动" in result.json()["error"]["message"]
    assert app.state.store.get("episode", episode["id"]) == episode
    assert not comfy.prompts


def test_same_workflow_model_config_refresh_keeps_story_and_user_prompts(system):
    client, app, comfy = system
    episode = preview(system, target_duration=1)
    body = edits(episode)
    body["shots"][0]["video_prompt"] = "User reviewed tracking shot"
    episode = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=body).json()
    client.patch(
        "/api/v1/workflows/default_video", json={"parameter_values": {"sampler.steps": 17}}
    )
    assert client.post(f"/api/v1/episodes/{episode['id']}/preview").status_code == 202
    refreshed = wait_preview(client, episode["id"])
    assert_story(episode, refreshed)
    assert refreshed["status"] == "AWAITING_REVIEW" and not comfy.prompts


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_partial_story_survives_rebind_and_only_missing_prompts_are_prepared(system, state):
    client, app, _ = system
    episode = preview(system)

    def partial(record):
        record["status"] = state
        record["shots"][-1]["prompts"] = None
        record["shots"][-1].pop("preview_prompt_view", None)

    original = app.state.store.update("episode", episode["id"], partial)
    result = rebind(client, original, image_workflow_id=imported(app)["id"])
    assert result.status_code == 200, result.text
    changed = result.json()
    assert_story(original, changed)
    assert changed["status"] == "DRAFT"
    assert client.post(f"/api/v1/episodes/{episode['id']}/preview").status_code == 202
    ready = wait_preview(client, episode["id"])
    assert ready["status"] == "AWAITING_REVIEW"
    assert ready["plan"] == original["plan"]
    assert ready["shots"][0]["prompts"] == original["shots"][0]["prompts"]
    assert ready["metrics"]["llm_calls"] == original["metrics"]["llm_calls"] + 1


def test_cloud_video_preview_keeps_timing_when_only_image_workflow_changes(system):
    client, _, comfy = system
    wid = install_cloud(client, comfy)
    episode = preview(
        system, target_duration=90, max_shot_duration=5, video_workflow_id=wid, memory_mode="low"
    )
    result = rebind(client, episode, image_workflow_id="default_image", reference_workflow_id=wid)
    assert result.status_code == 400  # Capability guard remains mandatory.
    _, app, _ = system
    result = rebind(client, episode, image_workflow_id=imported(app, "IMAGE_TO_IMAGE")["id"])
    assert result.status_code == 200, result.text
    assert_story(episode, result.json())
    assert result.json()["preview"]["remote_video"] is True
    assert result.json()["budget"]["max_duration"] == 5
    assert not comfy.prompts


def test_historical_jobs_from_old_binding_do_not_discard_new_unrendered_story(system):
    client, app, _ = system
    episode = preview(system)
    original = app.state.store.update("episode", episode["id"], {"workflow_binding_revision": 2})
    job = app.state.store.create(
        "job",
        {"status": "COMPLETED", "step_key": "binding:1:reference:style"},
        parent=episode["id"],
    )
    changed = rebind(client, original, image_workflow_id=imported(app)["id"])
    assert changed.status_code == 200, changed.text
    assert_story(original, changed.json())
    assert app.state.store.get("job", job["id"]) == job
