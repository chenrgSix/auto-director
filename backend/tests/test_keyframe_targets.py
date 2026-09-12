from copy import deepcopy

import httpx
import pytest
from PIL import Image, ImageDraw, ImageEnhance
from pydantic import ValidationError

from app.agents.directing import anchored_prompt
from app.agents.schemas import ShotPrompts
from app.generation.schemas import EpisodeRerun, PreviewShotUpdate
from app.media.keyframes import compare_keyframes
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_episode_rerun import complete, rerun
from tests.test_oom_recovery import interrupted


def test_render_target_precedes_identity_and_excludes_conflicting_history():
    bible = {
        "characters": [
            {
                "id": "cub",
                "description": "Three tigers always standing",
                "distinguishing_features": ["striped golden fur"],
            }
        ],
        "style": {"look": "cinematic"},
        "environment": {"time": "always daytime"},
        "continuity_rules": ["Exactly three tigers in every frame"],
    }
    before = deepcopy(bible)
    target = "Empty rock under moonlight. The last cub has disappeared into the closing portal."
    prompt = anchored_prompt(
        bible, target, {"state": "Three tigers roaring; no portal"}, stage="end_frame"
    )
    assert prompt.startswith("Requested end_frame target (highest priority):\n" + target)
    assert "striped golden fur" in prompt and "cinematic" in prompt and "<Picture 1>" in prompt
    for conflict in ["always standing", "always daytime", "Exactly three", "no portal"]:
        assert conflict not in prompt
    assert bible == before
    bible["style"]["look"] *= 30000
    assert target in anchored_prompt(bible, target, {}, stage="end_frame")


def frame(path, shift=0):
    image = Image.new("RGB", (256, 256), (60, 110, 130))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + shift, 25, 90 + shift, 170), fill=(210, 170, 60))
    draw.ellipse((130, 180, 220, 250), fill=(30, 60, 90))
    image.save(path)
    return image


def test_local_guard_detects_reencoding_and_small_color_change_but_allows_motion(tmp_path):
    start, end = tmp_path / "start.png", tmp_path / "end.png"
    original = frame(start)
    original.save(end)
    assert compare_keyframes(start, end)["near_duplicate"]
    ImageEnhance.Brightness(original).enhance(1.015).save(end, format="JPEG", quality=95)
    assert compare_keyframes(start, end)["near_duplicate"]
    frame(end, shift=45)
    assert not compare_keyframes(start, end)["near_duplicate"]
    Image.new("RGB", (256, 256), "black").save(start)
    Image.new("RGB", (256, 256), "white").save(end)
    assert not compare_keyframes(start, end)["near_duplicate"]


def clone_ends(system, failures):
    _, app, comfy = system
    original = comfy.handle
    state = {"ends": 0, "start": None}

    def handle(request):
        response = original(request)
        if request.url.path == "/view" and request.url.params["filename"].endswith(".png"):
            body = comfy.prompts.get(request.url.params["filename"].removesuffix(".png"))
            if body:
                job = app.state.store.get("job", body["client_id"])
                if job["type"] == "SHOT_START_FRAME":
                    state["start"] = response.content
                elif job["type"] == "SHOT_END_FRAME":
                    state["ends"] += 1
                    if failures is None or state["ends"] <= failures:
                        return httpx.Response(
                            200, content=state["start"], headers={"Content-Type": "image/png"}
                        )
        return response

    comfy.handle = handle
    return state


def generate(system, **extra):
    client, _, _ = system
    result = client.post(
        "/api/v1/episodes",
        json={
            "idea": "Walking cub exits through a portal",
            "target_duration": 1,
            "qa_enabled": False,
            "width": 256,
            "height": 256,
            "max_retries": 2,
            **extra,
        },
    )
    assert result.status_code == 201, result.text
    id = result.json()["id"]
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 202
    return wait_episode(client, id)


def test_duplicate_tail_retries_only_tail_before_video_even_without_vlm(system):
    client, app, _ = system
    clone_ends(system, 1)
    result = generate(system)
    assert result["status"] == "COMPLETED", result["error"]
    jobs = app.state.store.list("job", result["id"])
    assert sum(j["type"] == "SHOT_START_FRAME" for j in jobs) == 1
    assert sum(j["type"] == "SHOT_END_FRAME" for j in jobs) == 2
    assert sum(j["type"] == "SHOT_VIDEO" for j in jobs) == 1
    assert not result["shots"][0]["keyframe_comparison"]["near_duplicate"]
    ends = [j for j in jobs if j["type"] == "SHOT_END_FRAME"]
    assert len({j["patched_workflow"]["sampler"]["inputs"]["seed"] for j in ends}) == 2
    for job in jobs:
        for asset_id in job["output_asset_ids"]:
            assert client.get(f"/api/v1/assets/{asset_id}/file").status_code == 200
        if job["type"] == "SHOT_END_FRAME":
            prompt = job["patched_workflow"]["positive"]["inputs"]["text"]
            assert prompt.startswith("Requested end_frame target")
            assert "Lion has walked forward" in prompt
            assert "Same characters, location and lighting" not in prompt


def test_duplicate_exhaustion_stops_video_and_static_rerun_preserves_history(system):
    client, app, _ = system
    clone_ends(system, None)
    result = generate(system)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "KEYFRAMES_TOO_SIMILAR"
    jobs = app.state.store.list("job", result["id"])
    assert sum(j["type"] == "SHOT_START_FRAME" for j in jobs) == 1
    assert sum(j["type"] == "SHOT_END_FRAME" for j in jobs) == 3
    assert not any(j["type"] == "SHOT_VIDEO" for j in jobs)
    old = deepcopy(result["shots"])
    resumed = rerun(client, result, allow_static_end_frame=True)
    assert resumed["shots"][0]["prompts"]["allow_static_end_frame"]
    assert resumed["rerun_history"][-1]["shots"] == old
    assert resumed["shots"][0]["start_frame_asset_id"] == old[0]["start_frame_asset_id"]
    assert resumed["shots"][0]["end_frame_asset_id"] == old[0]["end_frame_asset_id"]


def test_explicit_end_asset_does_not_waste_retry_budget(system):
    client, app, comfy = system
    comfy.fixed_images = True
    asset = client.post(
        "/api/v1/assets", files={"file": ("same.png", comfy.image_bytes, "image/png")}
    ).json()
    result = generate(
        system,
        advanced_mode=True,
        workflow_overrides={
            "default_video": {"start.image": asset["id"], "end.image": asset["id"]},
        },
    )
    assert result["error"]["code"] == "KEYFRAMES_TOO_SIMILAR", result.get("error")
    assert not any(j["shot_id"] for j in app.state.store.list("job", result["id"]))


def test_preview_static_flag_roundtrip_legacy_omission_and_real_render(system):
    client, _, comfy = system
    comfy.fixed_images = True
    result = preview(system, target_duration=1)
    assert not result["shots"][0]["prompts"]["allow_static_end_frame"]
    body = edits(result)
    body["shots"][0]["allow_static_end_frame"] = True
    response = client.patch(f"/api/v1/episodes/{result['id']}/preview", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    response = client.patch(f"/api/v1/episodes/{result['id']}/preview", json=edits(result))
    result = response.json()
    assert result["shots"][0]["prompts"]["allow_static_end_frame"]
    assert (
        client.post(
            f"/api/v1/episodes/{result['id']}/approve", json={"expected_version": result["version"]}
        ).status_code
        == 202
    )
    result = wait_episode(client, result["id"])
    assert result["status"] == "COMPLETED", result["error"]


def test_static_rerun_override_does_not_change_dependent_shot_intent(system):
    client, _, _ = system
    before = complete(system, target_duration=2, max_shot_duration=1)
    result = rerun(client, before, shot_ids=[before["shots"][0]["id"]], allow_static_end_frame=True)
    assert result["shots"][0]["prompts"]["allow_static_end_frame"]
    assert not result["shots"][1]["prompts"]["allow_static_end_frame"]


async def test_static_flag_is_strict_and_old_prompts_default_to_motion():
    data = (await FakeProvider().generate_json("", {}, ShotPrompts)).model_dump()
    data.pop("allow_static_end_frame")
    assert not ShotPrompts.model_validate(data).allow_static_end_frame
    for invalid in ["false", 1]:
        with pytest.raises(ValidationError):
            ShotPrompts.model_validate({**data, "allow_static_end_frame": invalid})
        with pytest.raises(ValidationError):
            EpisodeRerun(expected_version=1, scope="video", allow_static_end_frame=invalid)
        with pytest.raises(ValidationError):
            PreviewShotUpdate(
                id="x",
                title="x",
                duration=1,
                start_frame_prompt="x",
                end_frame_prompt="y",
                video_prompt="z",
                allow_static_end_frame=invalid,
            )


def test_accepted_video_recovery_never_rejects_existing_endpoints(system, monkeypatch):
    client, _, _ = system
    failed, state = interrupted(system, ":video:small")

    async def forbidden(*args):
        raise AssertionError("Accepted video must resume without repeating keyframe checks")

    monkeypatch.setattr("app.generation.pipeline.inspect_keyframes", forbidden)
    state["offline"] = False
    assert client.post(f"/api/v1/episodes/{failed['id']}/generate").status_code == 202
    final = wait_episode(client, failed["id"])
    assert final["status"] == "COMPLETED", final["error"]


def test_i2v_never_compares_or_renders_an_end_frame(system, monkeypatch):
    from tests.test_capabilities import image_to_video_graph

    client, app, comfy = system
    comfy.fixed_images = True
    graph = image_to_video_graph(client.get("/api/v1/workflows/default_video").json()["workflow"])
    video = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "I2V guard fixture",
            "capability": "IMAGE_TO_VIDEO",
            "workflow": graph,
        },
    ).json()

    async def forbidden(*args):
        raise AssertionError("I2V has no tail to compare")

    monkeypatch.setattr("app.generation.pipeline.inspect_keyframes", forbidden)
    result = generate(system, video_workflow_id=video["id"])
    assert result["status"] == "COMPLETED", result["error"]
    assert not any(j["type"] == "SHOT_END_FRAME" for j in app.state.store.list("job", result["id"]))


@pytest.mark.parametrize("override", [False, True])
def test_custom_noise_seed_binding_reaches_patched_json_with_user_priority(system, override):
    client, app, comfy = system
    graph = client.get("/api/v1/workflows/default_image").json()["workflow"]
    graph["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": 1068100264298865}}
    comfy.info["RandomNoise"] = {"input": {"required": {"noise_seed": ["INT", {"min": 0}]}}}
    profile = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "Noise fixture",
            "capability": "TEXT_TO_IMAGE",
            "workflow": graph,
        },
    ).json()
    bindings = {**profile["bindings"], "seed": {"node_id": "noise", "input": "noise_seed"}}
    response = client.patch(f"/api/v1/workflows/{profile['id']}", json={"bindings": bindings})
    assert response.status_code == 200, response.text
    profile = response.json()
    assert (
        next(p for p in profile["parameters"] if p["key"] == "noise.noise_seed")["owner"]
        == "system"
    )
    result = generate(
        system,
        image_workflow_id=profile["id"],
        **(
            {
                "advanced_mode": True,
                "workflow_overrides": {profile["id"]: {"noise.noise_seed": 42}},
            }
            if override
            else {}
        ),
    )
    assert result["status"] == "COMPLETED", result["error"]
    jobs = [
        j
        for j in app.state.store.list("job", result["id"])
        if j["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"}
    ]
    values = {j["patched_workflow"]["noise"]["inputs"]["noise_seed"] for j in jobs}
    assert len(values) == (1 if override else 2)
    if override:
        assert values == {42}
    else:
        assert values == {result["seed"], result["seed"] + 1}
    assert (
        app.state.store.get("workflow", profile["id"])["workflow"]["noise"]["inputs"]["noise_seed"]
        == 1068100264298865
    )


def test_rerun_static_choice_survives_unprepared_shot_prompts(system):
    client, app, _ = system
    original = complete(system)
    episode = app.state.store.update(
        "episode", original["id"], lambda e: e["shots"][0].update(prompts=None)
    )
    result = rerun(client, episode, scope="keyframes", allow_static_end_frame=True)
    assert result["shots"][0]["prompts"]["allow_static_end_frame"]
    assert result["rerun_history"][-1]["shots"][0]["prompts"] is None
