import pytest

from app.core.config import ROOT
from app.core.errors import AppError
from app.db.store import Store
from app.workflows.manager import WorkflowManager
from app.workflows.router import CapabilityRouter
from app.workflows.schema import WorkflowCapability, WorkflowImport
from tests.test_api_capabilities import install_i2i
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_image_graph, image_to_video_graph


def test_router_resolves_all_capabilities_and_keeps_explicit_ids(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    router = CapabilityRouter(store)
    for capability in WorkflowCapability:
        base = store.get("workflow", f"default_{capability.media_type}")["workflow"]
        graph = (
            image_to_image_graph(base)
            if capability == "IMAGE_TO_IMAGE"
            else (image_to_video_graph(base) if capability == "IMAGE_TO_VIDEO" else base)
        )
        request = WorkflowImport(name=capability, capability=capability, workflow=graph)
        if capability == "REFERENCE_SEQUENCE_TO_VIDEO":
            request = WorkflowImport.model_validate_json(
                (ROOT / "workflow_examples/codex_h3_continuous/continuous.profile.json").read_text()
            )
        imported = manager.import_workflow(request)
        manager.set_default(imported["id"])
        assert router.resolve(capability)["id"] == imported["id"]
        assert router.select(capability.media_type)["id"] == imported["id"]
    assert router.resolve("TEXT_TO_IMAGE", "default_image")["id"] == "default_image"
    assert router.select("video", "default_video")["id"] == "default_video"
    with pytest.raises(AppError):
        router.resolve("IMAGE_TO_IMAGE", "default_image")
    with pytest.raises(AppError):
        router.select("image", "default_video")
    store.close()


def test_router_reads_capability_map_before_matching_legacy_fallback(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    router = CapabilityRouter(store)
    image = store.get("workflow", "default_image")
    custom = manager.import_workflow(
        WorkflowImport(
            name="capability default", capability="TEXT_TO_IMAGE", workflow=image["workflow"]
        )
    )
    store.update("settings", "settings", {"default_capabilities": {"TEXT_TO_IMAGE": custom["id"]}})
    assert router.resolve("TEXT_TO_IMAGE")["id"] == custom["id"]
    assert router.select("image")["id"] == custom["id"]
    assert router.resolve("FIRST_LAST_TO_VIDEO")["id"] == "default_video"
    with pytest.raises(AppError, match="IMAGE_TO_VIDEO"):
        router.resolve("IMAGE_TO_VIDEO")
    store.update("settings", "settings", {"default_image": None})
    assert router.select("image")["id"] == custom["id"]
    store.update(
        "settings", "settings", {"default_capabilities": {"TEXT_TO_IMAGE": "default_video"}}
    )
    with pytest.raises(AppError, match="不匹配"):
        router.resolve("TEXT_TO_IMAGE")
    store.update("settings", "settings", {"default_capabilities": {"TEXT_TO_IMAGE": "missing"}})
    with pytest.raises(AppError) as error:
        router.resolve("TEXT_TO_IMAGE")
    assert error.value.status == 404
    store.close()


def test_episode_uses_routed_defaults_but_pins_explicit_selection(system):
    client, app, comfy = system
    i2i = install_i2i(client, comfy)
    client.post(f"/api/v1/workflows/{i2i['id']}/default")
    video = client.get("/api/v1/workflows/default_video").json()
    i2v = client.post(
        "/api/v1/workflows/import",
        json={
            "name": "I2V routed",
            "capability": "IMAGE_TO_VIDEO",
            "workflow": image_to_video_graph(video["workflow"]),
        },
    ).json()
    client.post(f"/api/v1/workflows/{i2v['id']}/default")
    request = {"idea": "capability routing", "target_duration": 1, "width": 256, "height": 256}
    routed = client.post("/api/v1/episodes", json=request).json()
    assert (
        routed["image_workflow_id"],
        routed["video_workflow_id"],
        routed["reference_workflow_id"],
    ) == (i2i["id"], i2v["id"], "default_image")
    explicit = client.post(
        "/api/v1/episodes",
        json={
            **request,
            "image_workflow_id": "default_image",
            "video_workflow_id": "default_video",
        },
    ).json()
    assert (
        explicit["image_workflow_id"] == "default_image"
        and explicit["video_workflow_id"] == "default_video"
    )
    # Changing defaults after creation must not retarget the saved episode.
    client.post("/api/v1/workflows/default_image/default")
    client.post("/api/v1/workflows/default_video/default")
    id = routed["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    jobs = app.state.store.list("job", id)
    assert {j["workflow_id"] for j in jobs} == {"default_image", i2i["id"], i2v["id"]}
    assert not any(j["type"] == "SHOT_END_FRAME" for j in jobs)
