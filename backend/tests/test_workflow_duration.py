import json
from copy import deepcopy

import httpx
import pytest

from app.agents.directing import generation_budget
from app.core.errors import AppError
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import (
    analyze,
    check_value,
    patch,
    refresh_profile,
    validate_dependencies,
)
from app.workflows.duration import fit_duration, render_maximum
from app.workflows.ownership import canonicalize
from tests.test_agents import plan
from tests.test_api_pipeline import wait_episode


def cloud_workflow():
    graph = {
        "start": {"class_type": "LoadImage", "inputs": {"image": "start.png"}},
        "end": {"class_type": "LoadImage", "inputs": {"image": "end.png"}},
        "clip": {
            "class_type": "CloudVideo",
            "inputs": {
                "model": "base",
                "model.duration": 5,
                "model.prompt": "move",
                "model.resolution": "768P",
                "first_frame": ["start", 0],
                "last_frame": ["end", 0],
            },
        },
        "save": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["clip", 0], "filename_prefix": "test", "format": "mp4"},
        },
    }
    request = {
        "name": "Dynamic cloud video fixture",
        "capability": "FIRST_LAST_TO_VIDEO",
        "workflow": graph,
        "bindings": {
            "start_frame": {"node_id": "start", "input": "image"},
            "end_frame": {"node_id": "end", "input": "image"},
            "prompt": {"node_id": "clip", "input": "model.prompt"},
            "duration": {"node_id": "clip", "input": "model.duration"},
        },
        "outputs": {"video": "save"},
    }
    options = [
        {
            "key": model,
            "inputs": {
                "required": {
                    "prompt": ["STRING", {}],
                    "duration": ["INT", {"min": minimum, "max": 15, "step": step}],
                    "resolution": ["COMBO", {"options": resolutions}],
                }
            },
        }
        for model, minimum, step, resolutions in (
            ("base", 4, 1, ["768P", "2K"]),
            ("max", 5, 2, ["480P", "768P"]),
        )
    ]
    info = {
        "CloudVideo": {
            "api_node": True,
            "input": {
                "required": {
                    "model": ["COMFY_DYNAMICCOMBO_V3", {"options": options}],
                    "first_frame": ["IMAGE", {}],
                },
                "optional": {"last_frame": ["IMAGE", {}]},
            },
            "output": ["VIDEO"],
        },
        "LoadImage": {
            "input": {"required": {"image": ["STRING", {"image_upload": True}]}},
            "output": ["IMAGE"],
        },
        "SaveVideo": {"input": {"required": {"video": ["VIDEO", {}]}}},
    }
    return request, info


def cloud_profile():
    request, info = cloud_workflow()
    profile = canonicalize(
        {
            **analyze(request["workflow"]),
            **request,
            "id": "cloud",
            "type": "video",
            "capabilities": {"max_duration": 5},
            "parameter_values": {},
        }
    )
    return refresh_profile(profile, info), info


def test_dynamic_combo_reads_selected_branch_constraints_and_scalar_choices():
    profile, info = cloud_profile()
    items = {p["key"]: p for p in profile["parameters"]}
    assert items["clip.model"]["enum"] == ["base", "max"]
    duration = items["clip.model.duration"]
    assert (duration["type"], duration["min"], duration["max"], duration["step"]) == (
        "integer",
        4,
        15,
        1,
    )
    assert validate_dependencies(profile, info)["valid"]
    before = deepcopy(profile)
    changed = refresh_profile(profile, info, {"clip.model": "max"})
    changed_items = {p["key"]: p for p in changed["parameters"]}
    assert changed_items["clip.model.duration"]["min"] == 5
    assert changed_items["clip.model.resolution"]["enum"] == ["480P", "768P"]
    assert profile == before
    missing = deepcopy(profile)
    del missing["workflow"]["clip"]["inputs"]["model.duration"]
    report = validate_dependencies(missing, info)
    assert not report["valid"]
    assert any(p.get("field") == "model.duration" for p in report["issues"])


@pytest.mark.parametrize(("timeline", "expected"), [(1, 4), (2.86, 4), (4, 4), (4.2, 5), (5.0, 5)])
def test_timeline_rounds_up_to_legal_render_seconds_and_patches_integer(timeline, expected):
    profile, _ = cloud_profile()
    values, _, raw, _ = resolve_parameters(profile, {"duration": timeline}, {}, {}, False)
    assert values["timeline_duration"] == timeline
    assert values["duration"] == expected and type(values["duration"]) is int
    assert patch(profile, values, raw)["clip"]["inputs"]["model.duration"] == expected
    assert profile["workflow"]["clip"]["inputs"]["model.duration"] == 5


def test_cloud_video_ignores_vram_clip_ceiling_but_keeps_local_image_budget():
    profile, _ = cloud_profile()
    episode = {"quality": "fast", "aspect_ratio": "9:16", "max_shot_duration": 3}
    system = {"devices": [{"vram_free": 6 * 1024**3}]}
    budget = generation_budget(episode, profile["capabilities"], system, remote_video=True)
    assert budget["low_memory"] and budget["width"] == 384
    assert budget["max_duration"] == 3 and budget["render_max_duration"] == 5
    values, *_ = resolve_parameters(profile, {"duration": 2.86}, {}, {}, False, budget)
    assert values["duration"] == 4
    local = {**profile, "remote_video": False}
    with pytest.raises(AppError, match="无可用值"):
        render_maximum(local, budget)


def test_no_rounding_beyond_cap_and_overrides_remain_strict():
    profile, info = cloud_profile()
    profile["capabilities"]["max_duration"] = 4.8
    assert render_maximum(profile) == 4
    with pytest.raises(AppError, match="无可用值"):
        resolve_parameters(profile, {"duration": 4.2}, {}, {}, False)
    for illegal in (2.86, 3, True, "5"):
        with pytest.raises(AppError):
            resolve_parameters(profile, {"duration": 4}, {}, {"clip.model.duration": illegal}, True)
    profile["capabilities"]["max_duration"] = 10
    changed = refresh_profile(profile, info, {"clip.model": "max"})
    assert render_maximum(changed) == 9
    assert fit_duration(changed, 6, 10) == 7
    with pytest.raises(AppError, match="step"):
        resolve_parameters(changed, {"duration": 6}, {}, {"clip.model.duration": 6}, True)
    values, _, raw, _ = resolve_parameters(
        changed, {"duration": 7}, {}, {"clip.model": "max", "clip.model.duration": 7}, True
    )
    graph = patch(changed, values, raw)
    assert graph["clip"]["inputs"]["model"] == "max"
    assert graph["clip"]["inputs"]["model.duration"] == 7
    assert validate_dependencies({**changed, "workflow": graph}, info)["valid"]


def test_float_steps_and_numeric_duration_enums():
    profile, _ = cloud_profile()
    item = next(p for p in profile["parameters"] if p["role"] == "duration")
    item.update(type="number", min=1.5, max=5, step=0.5)
    assert fit_duration(profile, 2.86, 5) == 3
    with pytest.raises(AppError, match="step"):
        check_value(item, 2.86)
    item.update(type="select", enum=[4, 6, 10], min=None, max=None, step=None)
    assert fit_duration(profile, 4.2, 10) == 6
    assert fit_duration(profile, 9, 9, round_down=True) == 6


def install_cloud(client, comfy, *, remote=True):
    request, info = cloud_workflow()
    info["CloudVideo"]["api_node"] = remote
    comfy.info.update(info)
    response = client.post("/api/v1/workflows/import", json=request)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_validated_model_choices_and_advanced_overrides_reach_video_json(system):
    client, app, comfy = system
    wid = install_cloud(client, comfy)
    validated = client.post(f"/api/v1/workflows/{wid}/validate")
    assert validated.status_code == 200, validated.text
    assert validated.json()["validation"]["valid"]
    parameters = {p["key"]: p for p in validated.json()["parameters"]}
    assert parameters["clip.model"]["enum"] == ["base", "max"]
    assert parameters["clip.model.duration"]["min"] == 4
    eid = client.post(
        "/api/v1/episodes",
        json={
            "idea": "advanced model fixture",
            "target_duration": 10,
            "video_workflow_id": wid,
            "memory_mode": "low",
            "width": 256,
            "height": 256,
            "qa_enabled": False,
            "advanced_mode": True,
            "workflow_overrides": {wid: {"clip.model": "max", "clip.model.duration": 5}},
        },
    ).json()["id"]
    assert client.post(f"/api/v1/episodes/{eid}/generate").status_code == 202
    completed = wait_episode(client, eid)
    assert completed["status"] == "COMPLETED", completed.get("error")
    assert [s["duration"] for s in completed["shots"]] == [5, 5]
    videos = [j for j in app.state.store.list("job", eid) if j["type"] == "SHOT_VIDEO"]
    for job in videos:
        inputs = job["patched_workflow"]["clip"]["inputs"]
        assert inputs["model"] == "max" and inputs["model.duration"] == 5
        assert job["parameter_sources"]["clip.model.duration"] == "user"
    assert (
        client.get(f"/api/v1/workflows/{wid}").json()["workflow"]["clip"]["inputs"]["model"]
        == "base"
    )


def test_cloud_resume_retains_images_and_exact_timeline_with_real_ffmpeg(system):
    client, app, comfy = system
    wid = install_cloud(client, comfy)
    eid = client.post(
        "/api/v1/episodes",
        json={
            "idea": "hen lays an egg",
            "target_duration": 10,
            "video_workflow_id": wid,
            "width": 256,
            "height": 256,
            "qa_enabled": False,
            "memory_mode": "low",
        },
    ).json()["id"]
    timeline = [2.86, 2.12, 1.88, 1.72, 1.42]
    planned = plan(5).model_dump(mode="json")
    shots = []
    for i, shot in enumerate(planned["shots"]):
        shot["duration"] = timeline[i]
        shots.append(
            {
                **shot,
                "id": f"shot-{i}",
                "enabled": True,
                "status": "PENDING",
                "start_frame_asset_id": None,
                "end_frame_asset_id": None,
                "video_asset_id": None,
                "actual_end_frame_asset_id": None,
                "prompts": None,
                "qa": [],
                "error": None,
                "retry_version": 0,
            }
        )
    old_budget = generation_budget(
        {
            "quality": "standard",
            "aspect_ratio": "9:16",
            "memory_mode": "low",
            "width": 256,
            "height": 256,
        },
        {"max_duration": 5},
        {},
    )
    old_budget.pop("render_max_duration")
    app.state.store.update("episode", eid, {"plan": planned, "shots": shots, "budget": old_budget})
    original = comfy.handle
    reject_video = True

    def handle(request):
        if request.url.path == "/prompt" and request.method == "POST":
            graph = json.loads(request.content)["prompt"]
            if "clip" in graph:
                value = graph["clip"]["inputs"]["model.duration"]
                assert type(value) is int and value == 4
                if reject_video:
                    return httpx.Response(
                        400, json={"error": "fixture rejection before acceptance"}
                    )
        return original(request)

    comfy.handle = handle
    assert client.post(f"/api/v1/episodes/{eid}/generate").status_code == 202
    failed = wait_episode(client, eid)
    assert failed["status"] == "FAILED", failed.get("error")
    first = failed["shots"][0]
    assert first["start_frame_asset_id"] and first["end_frame_asset_id"]
    before = {j["id"] for j in app.state.store.list("job", eid) if j["status"] == "COMPLETED"}
    reject_video = False
    assert client.post(f"/api/v1/episodes/{eid}/generate").status_code == 202
    completed = wait_episode(client, eid)
    assert completed["status"] == "COMPLETED", completed.get("error")
    assert completed["budget"]["low_memory"] and completed["budget"]["max_duration"] == 5
    assert completed["budget"]["width"] == old_budget["width"]
    assert completed["references"] == failed["references"]
    for role in ("start_frame_asset_id", "end_frame_asset_id"):
        assert completed["shots"][0][role] == first[role]
    jobs = app.state.store.list("job", eid)
    assert before <= {j["id"] for j in jobs if j["status"] == "COMPLETED"}
    assert (
        len(
            [
                j
                for j in jobs
                if j["shot_id"] == "shot-0" and j["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"}
            ]
        )
        == 2
    )
    videos = [j for j in jobs if j["type"] == "SHOT_VIDEO" and j["status"] == "COMPLETED"]
    assert len(videos) == 5
    assert sorted(j["input_values"]["timeline_duration"] for j in videos) == sorted(timeline)
    assert all(j["patched_workflow"]["clip"]["inputs"]["model.duration"] == 4 for j in videos)
    assert [s["duration"] for s in completed["shots"]] == timeline
    assert completed["final_duration"] == pytest.approx(10, abs=0.15)


def test_incompatible_local_duration_fails_before_spending_on_images(system):
    client, _, comfy = system
    wid = install_cloud(client, comfy, remote=False)
    eid = client.post(
        "/api/v1/episodes",
        json={
            "idea": "invalid duration fixture",
            "target_duration": 10,
            "video_workflow_id": wid,
            "memory_mode": "low",
        },
    ).json()["id"]
    client.post(f"/api/v1/episodes/{eid}/generate")
    failed = wait_episode(client, eid)
    assert failed["status"] == "FAILED"
    assert "视频时长无可用值" in failed["error"]["message"]
    assert not comfy.prompts
    assert not client.get(f"/api/v1/jobs?episode_id={eid}").json()
