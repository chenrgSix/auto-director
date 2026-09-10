from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.agents.directing import Directors
from app.core.errors import AppError
from app.db.migrations import migrate
from app.db.store import Store
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import analyze, patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport, WorkflowPatch
from tests.fakes import FakeProvider
from tests.test_workflows import profile


def image_to_image_graph(base):
    graph = deepcopy(base)
    graph["reference"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "DO-NOT-USE-placeholder.png"},
        "_meta": {"title": "(Input:reference_image)"},
    }
    graph["encode"] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": ["reference", 0], "vae": ["model", 2]},
    }
    graph["sampler"]["inputs"]["latent_image"] = ["encode", 0]
    return graph


def image_to_video_graph(base):
    graph = deepcopy(base)
    del graph["end"]
    for node in graph.values():
        for field, value in node["inputs"].items():
            if isinstance(value, list) and value[0] == "end":
                node["inputs"][field] = ["start", value[1]]
    return graph


def test_capability_import_legacy_inference_and_media_mismatch(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    image = store.get("workflow", "default_image")
    video = store.get("workflow", "default_video")
    cases = [
        ("TEXT_TO_IMAGE", image["workflow"]),
        ("IMAGE_TO_IMAGE", image_to_image_graph(image["workflow"])),
        ("FIRST_LAST_TO_VIDEO", video["workflow"]),
        ("IMAGE_TO_VIDEO", image_to_video_graph(video["workflow"])),
    ]
    for capability, graph in cases:
        imported = manager.import_workflow(
            WorkflowImport(name=capability, capability=capability, workflow=graph)
        )
        assert imported["media_type"] == ("image" if capability.endswith("TO_IMAGE") else "video")
        assert imported["capability"] == capability
        assert not validate_bindings(imported)
    legacy = manager.import_workflow(
        WorkflowImport(name="legacy i2i", type="image", workflow=cases[1][1])
    )
    assert legacy["capability"] == "IMAGE_TO_IMAGE"
    with pytest.raises(ValidationError):
        WorkflowImport(
            name="mismatch",
            media_type="image",
            capability="IMAGE_TO_VIDEO",
            workflow=image["workflow"],
        )
    with pytest.raises(ValidationError):
        WorkflowImport(
            name="mismatch", media_type="video", type="image", workflow=image["workflow"]
        )
    store.close()


def test_owner_fill_workflow_defaults_user_precedence_and_immutable_graph(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    p = store.get("workflow", "default_image")
    p["workflow"]["motion"] = {
        "class_type": "Motion",
        "inputs": {"motion_strength": 0.1, "camera_motion": "template"},
        "_meta": {"title": "(Input:camera_motion) (Input:motion_strength)"},
    }
    p.update(analyze(p["workflow"]))
    p["parameter_values"] = {"sampler.steps": 18, "positive.text": "profile prompt"}
    owners = {x["key"]: x["owner"] for x in p["parameters"]}
    assert (
        owners["positive.text"] == owners["negative.text"] == owners["motion.camera_motion"] == "ai"
    )
    assert owners["latent.width"] == owners["latent.batch_size"] == "system"
    assert (
        owners["sampler.steps"] == owners["sampler.cfg"] == owners["model.ckpt_name"] == "workflow"
    )
    before = deepcopy(p)
    automatic = {
        "prompt": "AI prompt",
        "negative": "AI negative",
        "camera_motion": "tracking",
        "motion_strength": 0.7,
        "width": 512,
        "height": 768,
        "fps": 16,
        "batch": 1,
    }
    values, assets, raw, sources = resolve_parameters(
        p,
        automatic,
        {},
        {
            "positive.text": "user prompt",
            "sampler.steps": 9,
            "motion.motion_strength": 0.4,
            "latent.width": 640,
        },
        True,
    )
    graph = patch(p, values, raw)
    assert graph["positive"]["inputs"]["text"] == "user prompt"
    assert graph["negative"]["inputs"]["text"] == "AI negative"
    assert graph["sampler"]["inputs"]["steps"] == 9
    assert graph["sampler"]["inputs"]["cfg"] == p["workflow"]["sampler"]["inputs"]["cfg"]
    assert graph["motion"]["inputs"] == {"motion_strength": 0.4, "camera_motion": "tracking"}
    assert graph["latent"]["inputs"]["width"] == 640
    assert sources["positive.text"] == "user" and sources["negative.text"] == "ai"
    assert p == before and not assets
    automatic_graph = patch(p, automatic)
    assert automatic_graph["sampler"]["inputs"]["steps"] == 18
    assert automatic_graph["positive"]["inputs"]["text"] == "AI prompt"
    with pytest.raises(AppError, match="高级模式"):
        resolve_parameters(p, automatic, {}, {"positive.text": "x"}, False)
    locked = manager.update(
        "default_image",
        WorkflowPatch(
            parameter_rules={"positive.text": {"editable": False, "override_policy": "never"}}
        ),
    )
    with pytest.raises(AppError, match="不允许覆盖"):
        resolve_parameters(locked, automatic, {}, {"positive.text": "x"}, True)
    with pytest.raises(AppError, match="owner"):
        manager.update(
            "default_image", WorkflowPatch(parameter_rules={"positive.text": {"owner": "workflow"}})
        )
    custom = manager.update(
        "default_image", WorkflowPatch(parameter_rules={"sampler.steps": {"owner": "user"}})
    )
    assert next(p for p in custom["parameters"] if p["key"] == "sampler.steps")["owner"] == "user"
    store.close()


def test_duration_override_and_oom_constraints(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    p = store.get("workflow", "default_video")
    binding = p["bindings"]["duration"]
    key = f"{binding['node_id']}.{binding['input']}"
    fps_binding = p["bindings"]["fps"]
    fps_key = f"{fps_binding['node_id']}.{fps_binding['input']}"
    auto = {"prompt": "move", "duration": 3, "width": 512, "height": 512, "fps": 16, "batch": 1}
    values, _, raw, _ = resolve_parameters(p, auto, {}, {key: 73, fps_key: 24}, True)
    assert values["duration"] == 3 and values["fps"] == 24
    assert patch(p, values, raw)[binding["node_id"]]["inputs"][binding["input"]] == 73
    with pytest.raises(AppError, match="计划"):
        resolve_parameters(p, auto, {}, {key: 81}, True)
    with pytest.raises(AppError, match="上限"):
        resolve_parameters(p, {**auto, "duration": 6}, {}, {}, True)
    with pytest.raises(AppError, match="低显存"):
        resolve_parameters(
            p,
            auto,
            {},
            {"latent.width": 1024}
            if "latent.width" in {v["key"] for v in p["parameters"]}
            else {f"{p['bindings']['width']['node_id']}.{p['bindings']['width']['input']}": 1024},
            True,
            {"low_memory": True, "width": 512, "height": 512, "batch": 1, "max_duration": 5},
        )
    recovered, _, _, sources = resolve_parameters(
        p,
        {**auto, "width": 256, "height": 256, "duration": 1.5},
        {},
        {key: 49},
        True,
        recovery=True,
    )
    assert recovered["duration"] == 1.5 and sources[key] == "system:oom"
    start = p["bindings"]["start_frame"]
    start_key = f"{start['node_id']}.{start['input']}"
    _, assets, _, sources = resolve_parameters(
        p,
        auto,
        {"start_frame": "actual-segment-tail"},
        {start_key: "user-original-start"},
        True,
        recovery=True,
    )
    assert assets["start_frame"] == "actual-segment-tail"
    assert sources[start_key] == "system:oom"
    store.close()


async def test_plan_rejects_more_than_240_shots_before_model_call():
    provider = FakeProvider()
    episode = {"idea": "story", "target_duration": 600, "style": "film", "aspect_ratio": "9:16"}
    with pytest.raises(AppError) as error:
        await Directors(provider).plan(episode, 1)
    assert error.value.code == "LIMIT_EXCEEDED" and provider.usage["calls"] == 0
    result = await Directors(provider).plan(episode, 2.5)
    assert len(result.shots) == 240 and sum(s.duration for s in result.shots) == 600


async def test_director_honors_uniform_user_duration_without_changing_total():
    provider = FakeProvider()
    episode = {
        "idea": "story",
        "target_duration": 60,
        "style": "film",
        "aspect_ratio": "9:16",
        "fixed_shot_duration": 3,
    }
    result = await Directors(provider).plan(episode, 5)
    assert len(result.shots) == 20 and all(s.duration == 3 for s in result.shots)
    with pytest.raises(AppError) as error:
        await Directors(provider).plan({**episode, "target_duration": 10}, 5)
    assert error.value.code == "OVERRIDE_INVALID"


def test_migration_preserves_legacy_jobs_assets_plans_and_is_idempotent(tmp_path):
    store = Store(tmp_path)
    p = profile()
    p.update(id="legacy", parameter_values={"text-x.text": "keep"})
    store.create("workflow", p, id="legacy")
    graph = patch(p, {"prompt": "already submitted"})
    store.create(
        "job",
        {
            "profile_snapshot": p,
            "patched_workflow": graph,
            "comfy_prompt_id": "known",
            "status": "UNKNOWN",
        },
        id="job",
    )
    store.create(
        "episode",
        {
            "shots": [{"id": str(i)} for i in range(600)],
            "plan": {"keep": True},
            "references": {"style": "asset"},
            "video_parameters": {"x": 1},
        },
        id="ep",
    )
    migrate(store)
    migrated = store.get("workflow", "legacy")
    assert migrated["media_type"] == "image" and migrated["capability"] == "TEXT_TO_IMAGE"
    assert migrated["parameter_values"] == p["parameter_values"]
    assert all("owner" in item and "override_policy" in item for item in migrated["parameters"])
    job = store.get("job", "job")
    assert (
        job["patched_workflow"] == graph
        and job["comfy_prompt_id"] == "known"
        and job["status"] == "UNKNOWN"
    )
    episode = store.get("episode", "ep")
    assert (
        len(episode["shots"]) == 600
        and episode["references"] == {"style": "asset"}
        and episode["plan"] == {"keep": True}
    )
    assert episode["advanced_mode"] and episode["legacy_shot_limit"] == 600
    snapshots = [
        store.get(kind, id)
        for kind, id in [("workflow", "legacy"), ("job", "job"), ("episode", "ep")]
    ]
    migrate(store)
    assert snapshots == [
        store.get(kind, id)
        for kind, id in [("workflow", "legacy"), ("job", "job"), ("episode", "ep")]
    ]
    store.close()


def test_failed_migration_rolls_back_all_records(tmp_path):
    store = Store(tmp_path)
    original = store.create("workflow", profile(), id="valid")
    store.create("workflow", {**profile(), "capability": "INVALID"}, id="invalid")
    with pytest.raises(ValueError):
        migrate(store)
    assert store.get("workflow", "valid") == original
    with store.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar() == 0
    store.close()


def test_media_inputs_cannot_be_disguised_as_prompt_or_workflow_owned():
    from app.workflows.ownership import canonicalize

    p = profile()
    p["workflow"]["file"] = {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}}
    p.update(analyze(p["workflow"]))
    p["bindings"]["prompt"] = {"node_id": "file", "input": "image"}
    assert any("素材输入" in issue["message"] for issue in validate_bindings(p))
    p["bindings"].pop("prompt")
    p["parameter_rules"] = {"file.image": {"owner": "workflow"}}
    with pytest.raises(AppError, match="owner"):
        canonicalize(p)
