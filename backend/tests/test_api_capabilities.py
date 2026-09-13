from copy import deepcopy

from app.agents.schemas import QAResult
from tests.fakes import FakeProvider
from tests.media_assertions import assert_full_duration
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_image_graph, image_to_video_graph


def install_i2i(client, comfy):
    graph = image_to_image_graph(client.get("/api/v1/workflows/default_image").json()["workflow"])
    comfy.info["VAEEncode"] = {"input": {"required": {"pixels": ["IMAGE", {}], "vae": ["VAE", {}]}}}
    result = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "Image reference fixture",
            "media_type": "image",
            "capability": "IMAGE_TO_IMAGE",
            "workflow": graph,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def test_i2i_pipeline_bootstraps_real_references_and_keeps_continuity(system):
    client, app, comfy = system
    p = install_i2i(client, comfy)
    assert client.post(f"/api/v1/workflows/{p['id']}/default").status_code == 200
    settings = client.get("/api/v1/settings").json()
    assert settings["default_capabilities"]["TEXT_TO_IMAGE"] == "default_image"
    assert settings["limits"]["episode_shots"] == 240
    result = client.post(
        "/api/v1/episodes",
        json={
            "idea": "reference test",
            "target_duration": 5,
            "width": 256,
            "height": 256,
            "max_shot_duration": 3,
        },
    )
    id = result.json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    jobs = app.state.store.list("job", id)
    reference_jobs = [j for j in jobs if j["type"].endswith("_REFERENCE")]
    assert reference_jobs and all(
        j["profile_snapshot"]["capability"] == "TEXT_TO_IMAGE" for j in reference_jobs
    )
    image_jobs = [j for j in jobs if j["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"}]
    assert image_jobs and all(
        j["profile_snapshot"]["capability"] == "IMAGE_TO_IMAGE" for j in image_jobs
    )
    for job in image_jobs:
        asset_id = job["asset_bindings"]["reference_image"]
        assert app.state.store.get("asset", asset_id)["episode_id"] == id
        assert app.state.assets.path(asset_id).is_file()
        assert job["patched_workflow"]["reference"]["inputs"]["image"] == "autodirector/fixture.png"
        assert job["parameter_sources"]["reference.image"] == "asset_resolver"
    end = next(
        j
        for j in image_jobs
        if j["type"] == "SHOT_END_FRAME" and j["shot_id"] == episode["shots"][0]["id"]
    )
    assert end["asset_bindings"]["reference_image"] == episode["shots"][0]["start_frame_asset_id"]
    assert (
        episode["shots"][1]["start_frame_asset_id"]
        == episode["shots"][0]["actual_end_frame_asset_id"]
    )


def test_i2v_advanced_overrides_fill_owners_and_take_priority(system):
    client, app, _ = system
    graph = image_to_video_graph(client.get("/api/v1/workflows/default_video").json()["workflow"])
    result = client.post(
        "/api/v1/workflows/import",
        json={"name": "I2V fixture", "capability": "IMAGE_TO_VIDEO", "workflow": graph},
    )
    assert result.status_code == 201, result.text
    p = result.json()

    def key(role):
        binding = p["bindings"][role]
        return f"{binding['node_id']}.{binding['input']}"

    overrides = {
        key("duration"): 49,
        key("fps"): 16,
        key("width"): 256,
        key("height"): 256,
        "positive.text": "User camera prompt",
        "negative.text": "User negatives",
        "sampler.steps": 7,
    }
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "override test",
            "target_duration": 6,
            "advanced_mode": True,
            "width": 256,
            "height": 256,
            "video_workflow_id": p["id"],
            "workflow_overrides": {p["id"]: overrides},
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    assert [s["duration"] for s in episode["shots"]] == [3, 3]
    jobs = [j for j in app.state.store.list("job", id) if j["type"] == "SHOT_VIDEO"]
    assert len(jobs) == 2
    assert not any(j["type"] == "SHOT_END_FRAME" for j in app.state.store.list("job", id))
    assert all(s["end_frame_asset_id"] is None for s in episode["shots"])
    assert all(s["actual_end_frame_asset_id"] for s in episode["shots"])
    assert (
        episode["shots"][1]["start_frame_asset_id"]
        == episode["shots"][0]["actual_end_frame_asset_id"]
    )
    keyframe_qa = [q for q in app.state.store.list("qa", id) if q["stage"] == "keyframes"]
    assert len(keyframe_qa) == 2 and all(len(q["asset_ids"]) == 1 for q in keyframe_qa)
    for job in jobs:
        assert job["input_values"]["prompt"] == "User camera prompt"
        assert job["patched_workflow"]["sampler"]["inputs"]["steps"] == 7
        assert (
            job["parameter_sources"]["positive.text"]
            == job["parameter_sources"][key("duration")]
            == "user"
        )
        assert job["profile_snapshot"]["capability"] == "IMAGE_TO_VIDEO"
        assert "end_frame" not in job["profile_snapshot"]["bindings"]
        assert "end_frame" not in job["asset_bindings"]
    assert_full_duration(app, episode)


def test_asset_overrides_are_uploaded_real_media_and_reflected_in_shot(system):
    client, app, comfy = system
    uploaded = client.post(
        "/api/v1/assets", files={"file": ("custom.png", comfy.image_bytes, "image/png")}
    ).json()
    p = client.get("/api/v1/workflows/default_video").json()
    binding = p["bindings"]["start_frame"]
    key = f"{binding['node_id']}.{binding['input']}"
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "override asset",
            "target_duration": 1,
            "advanced_mode": True,
            "width": 256,
            "height": 256,
            "workflow_overrides": {p["id"]: {key: uploaded["id"]}},
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    assert episode["shots"][0]["start_frame_asset_id"] == uploaded["id"]
    job = next(j for j in app.state.store.list("job", id) if j["type"] == "SHOT_VIDEO")
    assert job["asset_bindings"]["start_frame"] == uploaded["id"]
    assert job["parameter_sources"][key] == "user"
    assert (
        job["patched_workflow"][binding["node_id"]]["inputs"][binding["input"]]
        == "autodirector/fixture.png"
    )


def test_i2v_transition_qa_retries_start_frame_without_end_render(system):
    client, app, _ = system

    class FailTransitionOnce(FakeProvider):
        failed = False

        async def generate_json(self, system, context, schema, *, images=None):
            result = await super().generate_json(system, context, schema, images=images)
            if schema is QAResult and context["stage"] == "video" and not self.failed:
                self.failed = True
                result.transition_quality = 0.2
            return result

    app.state.generation.provider_factory = FailTransitionOnce
    graph = image_to_video_graph(client.get("/api/v1/workflows/default_video").json()["workflow"])
    profile = client.post(
        "/api/v1/workflows/import",
        json={"name": "I2V QA", "capability": "IMAGE_TO_VIDEO", "workflow": graph},
    ).json()
    id = client.post(
        "/api/v1/episodes",
        json={
            "idea": "transition retry",
            "target_duration": 1,
            "width": 256,
            "height": 256,
            "video_workflow_id": profile["id"],
        },
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    jobs = app.state.store.list("job", id)
    assert sum(j["type"] == "SHOT_START_FRAME" for j in jobs) == 2
    assert sum(j["type"] == "SHOT_VIDEO" for j in jobs) == 2
    assert not any(j["type"] == "SHOT_END_FRAME" for j in jobs)


def test_override_modes_missing_assets_and_rule_rebinding(system):
    client, _, comfy = system
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "bad",
            "target_duration": 5,
            "advanced_mode": False,
            "workflow_overrides": {"default_image": {"positive.text": "x"}},
        },
    )
    assert response.status_code == 422
    p = install_i2i(client, comfy)
    missing = client.post(f"/api/v1/workflows/{p['id']}/test-run", json={"values": {"prompt": "x"}})
    assert missing.status_code == 400
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "bad asset",
            "target_duration": 5,
            "advanced_mode": True,
            "image_workflow_id": p["id"],
            "workflow_overrides": {p["id"]: {"reference.image": "placeholder.png"}},
        },
    )
    assert response.status_code == 404
    original = client.get("/api/v1/workflows/default_image").json()
    bindings = deepcopy(original["bindings"])
    bindings["camera_motion"] = bindings.pop("prompt")
    changed = client.patch("/api/v1/workflows/default_image", json={"bindings": bindings}).json()
    item = next(p for p in changed["parameters"] if p["key"] == "positive.text")
    assert item["role"] == "camera_motion" and item["owner"] == "ai"
    after = client.post("/api/v1/workflows/default_image/validate").json()
    item = next(p for p in after["parameters"] if p["key"] == "positive.text")
    assert item["role"] == "camera_motion" and item["owner"] == "ai"
    assert not after["validation"]["valid"]


def test_plan_limit_and_wrong_media_fail_before_render_submission(system):
    client, app, comfy = system
    id = client.post(
        "/api/v1/episodes",
        json={"idea": "too many shots", "target_duration": 241, "max_shot_duration": 1},
    ).json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "FAILED" and episode["error"]["code"] == "LIMIT_EXCEEDED"
    assert episode["metrics"]["llm_calls"] == 0 and not app.state.store.list("job", id)
    image = client.post(
        "/api/v1/assets", files={"file": ("image.png", comfy.image_bytes, "image/png")}
    ).json()
    import pytest

    from app.core.errors import AppError
    from app.generation.resolvers import AssetResolver

    with pytest.raises(AppError, match="video"):
        AssetResolver(app.state.store, app.state.assets).validate(
            "reference_video", image["id"], "workflow-tests"
        )
    with pytest.raises(AppError, match="不属于"):
        AssetResolver(app.state.store, app.state.assets).validate(
            "reference_image", image["id"], "another-episode"
        )
