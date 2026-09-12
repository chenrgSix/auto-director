import threading

import pytest

from app.agents.shot_batch import ShotPromptBatch
from tests.fakes import FakeProvider
from tests.test_ai_parameters import CreativeProvider, configure_ai_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import preview
from tests.test_episode_workflows import imported


@pytest.mark.parametrize("image_capability", ["TEXT_TO_IMAGE", "IMAGE_TO_IMAGE"])
@pytest.mark.parametrize("video_capability", ["FIRST_LAST_TO_VIDEO", "IMAGE_TO_VIDEO"])
def test_batches_reach_all_capabilities_and_keep_override_and_approval_rules(
    system, image_capability, video_capability
):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    comfy.info["VAEEncode"] = {"input": {"required": {"pixels": ["IMAGE", {}], "vae": ["VAE", {}]}}}
    image = imported(app, image_capability)
    video = imported(app, video_capability)
    for profile in (image, video):
        response = client.patch(
            f"/api/v1/workflows/{profile['id']}",
            json={
                "parameter_rules": {"sampler.denoise": {"owner": "ai"}},
            },
        )
        assert response.status_code == 200, response.text
    app.state.generation.provider_factory = CreativeProvider
    prepared = preview(
        system,
        target_duration=6,
        max_shot_duration=2,
        image_workflow_id=image["id"],
        video_workflow_id=video["id"],
        advanced_mode=True,
        workflow_overrides={video["id"]: {"sampler.denoise": 0.9}},
    )
    id = prepared["id"]
    assert len(prepared["shots"]) == 3
    assert prepared["prompt_preparation"]["requests"] == 1
    assert prepared["prompt_preparation"]["video_capability"] == video_capability
    assert not comfy.prompts
    progress = client.get(f"/api/v1/episodes/{id}/progress").json()
    assert progress["phase"] == "preview" and progress["percent"] == 100
    assert progress["shots_passed"] == 0
    assert progress["preparation"]["completed"] == progress["preparation"]["total"] == 3
    assert (
        client.post(
            f"/api/v1/episodes/{id}/approve", json={"expected_version": prepared["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final["error"]
    assert final["plan"] == prepared["plan"]
    assert [s["prompts"] for s in final["shots"]] == [s["prompts"] for s in prepared["shots"]]
    jobs = app.state.store.list("job", id)
    for job in jobs:
        if job["type"].endswith("_REFERENCE"):
            continue
        is_video = job["workflow_id"] == video["id"]
        assert job["ai_parameter_values"]["sampler.denoise"] == 0.6
        assert job["patched_workflow"]["sampler"]["inputs"]["denoise"] == (0.9 if is_video else 0.6)
        assert job["parameter_sources"]["sampler.denoise"] == ("user" if is_video else "ai")
        if not is_video and image_capability == "IMAGE_TO_IMAGE":
            assert (
                job["patched_workflow"]["reference"]["inputs"]["image"]
                != "DO-NOT-USE-placeholder.png"
            )
    if video_capability == "IMAGE_TO_VIDEO":
        assert not any(job["type"] == "SHOT_END_FRAME" for job in jobs)
    else:
        assert any(job["type"] == "SHOT_END_FRAME" for job in jobs)
    assert client.get(f"/api/v1/episodes/{id}/progress").json()["phase"] == "generation"


def test_pending_batch_progress_is_read_only_and_cancel_can_resume_in_single_mode(system):
    client, app, comfy = system
    entered, release = threading.Event(), threading.Event()

    class DelayedSecond(FakeProvider):
        def __init__(self):
            super().__init__()
            self.batches = 0

        async def generate_json(self, system, context, schema, *, images=None):
            if issubclass(schema, ShotPromptBatch):
                self.batches += 1
                if self.batches == 2:
                    entered.set()
                    import asyncio

                    await asyncio.to_thread(release.wait, 5)
            return await super().generate_json(system, context, schema, images=images)

    app.state.generation.provider_factory = DelayedSecond
    created = client.post(
        "/api/v1/episodes",
        json={
            "idea": "batch cancel",
            "target_duration": 12,
            "max_shot_duration": 2,
            "preview_required": True,
        },
    ).json()
    id = created["id"]
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    try:
        assert entered.wait(4)
        partial = client.get(f"/api/v1/episodes/{id}").json()
        state = client.get(f"/api/v1/episodes/{id}/progress").json()
        assert state["phase"] == "preview" and state["percent"] == 50
        assert len(state["preparation"]["active_shot_ids"]) == 3
        assert state["preparation"]["completed"] == 3
        assert (
            client.post(
                f"/api/v1/episodes/{id}/approve", json={"expected_version": partial["version"]}
            ).status_code
            == 409
        )
        assert client.patch("/api/v1/settings", json={"prompt_batch_size": 1}).status_code == 409
        assert client.post(f"/api/v1/episodes/{id}/cancel").status_code == 200
    finally:
        release.set()
    client.portal.call(app.state.generation.queue.join)
    cancelled = client.get(f"/api/v1/episodes/{id}").json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["shots"][:3] == partial["shots"][:3]
    assert not any(s["prompts"] for s in cancelled["shots"][3:])
    assert client.patch("/api/v1/settings", json={"prompt_batch_size": 1}).status_code == 200
    app.state.generation.provider_factory = FakeProvider
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 202
    client.portal.call(app.state.generation.queue.join)
    ready = client.get(f"/api/v1/episodes/{id}").json()
    assert ready["status"] == "AWAITING_REVIEW", ready["error"]
    assert ready["prompt_preparation"]["requests"] == 3
    assert [s["prompts"] for s in ready["shots"][:3]] == [
        s["prompts"] for s in partial["shots"][:3]
    ]
    assert not comfy.prompts
