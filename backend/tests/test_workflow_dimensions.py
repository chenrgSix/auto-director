from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.generation.parameters import fit_budget_dimensions, resolve_parameters
from app.workflows.analyzer import check_value, patch
from app.workflows.dimensions import fit_dimensions
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import preview
from tests.test_episode_rerun import complete
from tests.test_workflows import profile


def constrained_profile(**constraints):
    result = profile()
    for item in result["parameters"]:
        if item.get("role") == "height":
            item.update({"min": 32, "max": 2048, "step": 32, **constraints})
    return result


@pytest.mark.parametrize(
    "constraints,expected",
    [
        ({}, 672),
        ({"step": 64}, 672),
        ({"min": 16, "step": 64}, 656),
        ({"max": 600}, 576),
        ({"enum": [512, 768]}, 512),
    ],
)
def test_automatic_dimensions_fit_actual_step_origin_range_and_enum(constraints, expected):
    p = constrained_profile(**constraints)
    before = deepcopy(p)
    values = fit_dimensions(p, {"width": 384, "height": 688})
    assert values == {"width": 384, "height": expected}
    graph = patch(p, values)
    assert graph["size-y"]["inputs"]["height"] == expected
    assert p == before


@pytest.mark.parametrize("constraints", [{"min": 704}, {"enum": [768]}])
def test_impossible_dimension_does_not_increase_memory_budget(constraints):
    with pytest.raises(AppError, match="没有合法尺寸"):
        fit_dimensions(constrained_profile(**constraints), {"height": 688})


def test_fit_respects_ceiling_and_leaves_unbound_dimensions_and_template_unchanged():
    p = constrained_profile()
    assert fit_dimensions(p, {"height": 688}, ceilings={"height": 600})["height"] == 576
    del p["bindings"]["height"]
    assert fit_dimensions(p, {"height": 688})["height"] == 688


@pytest.mark.parametrize("source", ["episode", "advanced", "trial", "ai"])
def test_explicit_invalid_dimension_is_rejected_instead_of_rounded(source):
    p = constrained_profile()
    automatic = {"width": 384, "height": 688}
    if source == "episode":
        with pytest.raises(AppError, match="step"):
            fit_budget_dimensions({"height": 688}, p, automatic)
        return
    if source == "ai":
        next(item for item in p["parameters"] if item.get("role") == "height")["owner"] = "ai"
    with pytest.raises(AppError):
        resolve_parameters(
            p,
            automatic,
            {},
            {"size-y.height": 688} if source == "advanced" else {},
            True,
            None if source == "trial" else {**automatic, "max_duration": 5},
            ai_values={"size-y.height": 688} if source == "ai" else {},
        )


def test_valid_override_wins_and_oom_dimensions_align_after_override():
    p = constrained_profile()
    budget = {"width": 384, "height": 688, "max_duration": 5, "low_memory": True}
    values, _, raw, sources = resolve_parameters(
        p, budget, {}, {"size-y.height": 640}, True, budget
    )
    assert values["height"] == 640 and sources["size-y.height"] == "user"
    assert patch(p, values, raw)["size-y"]["inputs"]["height"] == 640
    smaller, _, _, sources = resolve_parameters(
        p, {**budget, "height": 496}, {}, {"size-y.height": 640}, True, budget, recovery=True
    )
    assert smaller["height"] == 480
    assert sources["size-y.height"] == "system:oom"


def configure_dimensions(client, comfy, minimum=32):
    p = client.get("/api/v1/workflows/default_video").json()
    for role in ("width", "height"):
        b = p["bindings"][role]
        node_type = p["workflow"][b["node_id"]]["class_type"]
        comfy.info[node_type]["input"]["required"][b["input"]] = [
            "INT",
            {"min": minimum, "max": 2048, "step": 32},
        ]
    return p


def test_preview_approval_and_ffmpeg_use_aligned_dimensions(system):
    client, app, comfy = system
    p = configure_dimensions(client, comfy)
    episode = preview(system, width=None, height=None, memory_mode="low", max_shot_duration=5)
    assert episode["budget"]["width"] == 384 and episode["budget"]["height"] == 672
    assert not comfy.prompts
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/approve", json={"expected_version": episode["version"]}
    )
    assert response.status_code == 202
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    job = next(j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SHOT_VIDEO")
    b = p["bindings"]["height"]
    assert job["patched_workflow"][b["node_id"]]["inputs"][b["input"]] == 672
    asset = app.state.store.get("asset", final["final_video_asset_id"])
    assert (asset["metadata"]["video"]["width"], asset["metadata"]["video"]["height"]) == (384, 672)
    assert abs(final["final_duration"] - 5) < 0.1


@pytest.mark.parametrize("explicit", [False, True])
def test_dimension_preflight_fails_before_any_reference_or_keyframe(system, explicit):
    client, app, comfy = system
    configure_dimensions(client, comfy, minimum=32 if explicit else 704)
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "bad size",
            "memory_mode": "low",
            "target_duration": 1,
            **({"width": 384, "height": 688} if explicit else {}),
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "FAILED"
    assert episode["error"]["code"] == "WORKFLOW_INVALID"
    assert not comfy.prompts and not app.state.store.list("asset", id)
    assert not app.state.store.list("job", id)


def test_legacy_failed_video_resumes_with_saved_frames_and_repaired_budget(system):
    client, app, comfy = system
    old = complete(system, width=None, height=None, memory_mode="low")
    assert old["budget"]["height"] == 688
    configure_dimensions(client, comfy)

    def fail(episode):
        episode.update(status="FAILED", final_video_asset_id=None)
        episode["shots"][0].update(status="FAILED", video_asset_id=None)

    app.state.store.update("episode", old["id"], fail)
    submissions = len(comfy.prompts)
    client.post(f"/api/v1/episodes/{old['id']}/generate")
    final = wait_episode(client, old["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    assert final["budget"]["height"] == 672
    assert len(comfy.prompts) == submissions + 1
    for key in ("plan", "bible", "references"):
        assert final[key] == old[key]
    for key in ("start_frame_asset_id", "end_frame_asset_id", "prompts", "duration"):
        assert final["shots"][0][key] == old["shots"][0][key]
    assert final["metrics"]["llm_calls"] == old["metrics"]["llm_calls"]


async def test_engine_rechecks_live_dimensions_before_patch(system):
    client, app, comfy = system
    p = app.state.store.get("workflow", "default_image")
    budget = {"width": 384, "height": 688, "max_duration": 5, "low_memory": True}
    job = app.state.engine.create_job(
        p, budget, {}, "dimension-fixture", None, "SHOT_START_FRAME", budget=budget
    )
    binding = p["bindings"]["height"]
    node_type = p["workflow"][binding["node_id"]]["class_type"]
    comfy.info[node_type]["input"]["required"][binding["input"]] = ["INT", {"min": 32, "step": 32}]
    await app.state.engine.run(job["id"])
    result = app.state.store.get("job", job["id"])
    assert result["input_values"]["height"] == 672
    assert result["patched_workflow"][binding["node_id"]]["inputs"][binding["input"]] == 672
    parameter = {"key": "height", "field": "height", "type": "integer", "min": 32, "step": 32}
    check_value(parameter, result["input_values"]["height"])
