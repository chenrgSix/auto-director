import time
from copy import deepcopy
from uuid import uuid4

from tests.test_creation_mcp import call, connect
from tests.test_creation_packages import BASE, create, document, submit

ROOT = "/api/v1/episodes"


def wait_gate(client, app, id, completed=False):
    for _ in range(800):
        episode = client.get(f"{ROOT}/{id}").json()
        if (
            episode["status"] in {"AWAITING_IMAGE_REVIEW", "COMPLETED", "FAILED"}
            and id not in app.state.generation.busy
        ):
            assert episode["status"] == ("COMPLETED" if completed else "AWAITING_IMAGE_REVIEW"), (
                episode.get("error")
            )
            return episode
        time.sleep(0.01)
    raise AssertionError("image gate timeout")


def setup(client):
    package = document()
    package["brief"]["image_review_required"] = True
    project, _ = create(client, package)
    delivered, _ = submit(client, project)
    id = delivered["episode_id"]
    version = client.get(f"{ROOT}/{id}").json()["version"]
    response = client.post(
        f"{BASE}/{project}/productions/{id}/confirm",
        json={"request_id": str(uuid4()), "expected_version": version, "confirm": True},
    )
    assert response.status_code == 202, response.text
    return project, id


def decision(client, id, **change):
    context = client.get(f"{ROOT}/{id}/image-review").json()
    return {
        "request_id": str(uuid4()),
        "expected_version": context["version"],
        "review_key": context["review_key"],
        "decision": "approve",
        "notes": "已检查当前图的主体、构图、道具与动作端点。",
        **change,
    }


def post(client, id, body):
    response = client.post(f"{ROOT}/{id}/image-review", json=body)
    assert response.status_code == 202, response.text
    return response


def test_reference_and_per_shot_gates_block_video_and_reuse_actual_tail(system):
    client, app, comfy = system
    app.state.generation.provider_factory = lambda: (_ for _ in ()).throw(
        AssertionError("No model API")
    )
    _, id = setup(client)
    episode = wait_gate(client, app, id)
    assert len(episode["references"]) == 3
    assert not any(s["start_frame_asset_id"] or s["video_asset_id"] for s in episode["shots"])
    assert client.post(f"{ROOT}/{id}/generate").status_code == 409
    assert not app.state.generation.busy
    body = decision(client, id)
    post(client, id, body)
    episode = wait_gate(client, app, id)
    assert episode["shots"][0]["start_frame_asset_id"] and not any(
        s["video_asset_id"] for s in episode["shots"]
    )
    calls = len(comfy.calls)
    post(client, id, body)  # Lost response retry cannot queue again.
    assert len(comfy.calls) == calls and not app.state.generation.busy
    conflict = client.post(f"{ROOT}/{id}/image-review", json={**body, "notes": "different"})
    assert conflict.status_code == 409
    post(client, id, decision(client, id))
    episode = wait_gate(client, app, id)
    first, second = episode["shots"]
    assert first["video_asset_id"] and not second["video_asset_id"]
    assert second["start_frame_asset_id"] == first["actual_end_frame_asset_id"]
    request = decision(
        client, id, decision="revise", target="start_frame", prompt="Illegal detached continuation"
    )
    assert client.post(f"{ROOT}/{id}/image-review", json=request).status_code == 409
    post(client, id, decision(client, id))
    episode = wait_gate(client, app, id, completed=True)
    assert len(episode["image_review_history"]) == 3 and episode["final_video_asset_id"]


def test_revision_retains_media_and_package_rejects_stale_confirmation(system):
    client, app, _ = system
    project, id = setup(client)
    episode = wait_gate(client, app, id)
    original = deepcopy(client.get(f"{BASE}/{project}/export").json())
    old = episode["references"]["environment"]
    stale = decision(client, id)
    post(
        client,
        id,
        decision(
            client,
            id,
            decision="revise",
            target="environment",
            prompt="An empty forest clearing in soft morning light",
        ),
    )
    episode = wait_gate(client, app, id)
    assert episode["references"]["environment"] != old
    assert client.get(f"/api/v1/assets/{old}/file").status_code == 200
    assert client.post(f"{ROOT}/{id}/image-review", json=stale).status_code == 409
    post(client, id, decision(client, id))
    episode = wait_gate(client, app, id)
    old_start = episode["shots"][0]["start_frame_asset_id"]
    old_end = episode["shots"][0]["end_frame_asset_id"]
    post(
        client,
        id,
        decision(
            client,
            id,
            decision="revise",
            target="end_frame",
            prompt="Lion reaches the far right edge of the clearing",
        ),
    )
    episode = wait_gate(client, app, id)
    assert episode["shots"][0]["start_frame_asset_id"] == old_start
    assert episode["shots"][0]["end_frame_asset_id"] != old_end
    assert not any(s["video_asset_id"] for s in episode["shots"])
    assert client.get(f"{BASE}/{project}/export").json() == original
    current = decision(client, id)
    app.state.store.update(
        "workflow",
        episode["image_workflow_id"],
        lambda w: w.update(configuration_version=w["version"] + 1),
    )
    current["expected_version"] = app.state.store.get("episode", id)["version"]
    assert client.post(f"{ROOT}/{id}/image-review", json=current).status_code == 409


def test_official_mcp_client_inspects_and_decides_pending_images(system):
    client, app, _ = system
    project, id = setup(client)
    wait_gate(client, app, id)

    async def exercise():
        async with connect(app) as session:
            result = await session.call_tool(
                "inspect_preproduction_images", {"project_id": project, "episode_id": id}
            )
            assert not result.isError and len([c for c in result.content if c.type == "image"]) == 3
            current = result.structuredContent
            return await call(
                session,
                "decide_preproduction_images",
                {
                    "project_id": project,
                    "episode_id": id,
                    "request": {
                        "request_id": str(uuid4()),
                        "expected_version": current["version"],
                        "review_key": current["review_key"],
                        "decision": "approve",
                        "notes": "已观察三张参考，角色与环境布局可用。",
                    },
                },
            )

    result = client.portal.call(exercise)
    assert result["status"] == "QUEUED"
    episode = wait_gate(client, app, id)
    assert episode["shots"][0]["start_frame_asset_id"] and not episode["shots"][0]["video_asset_id"]


def test_waiting_review_survives_worker_restart_and_releases_queue(system):
    client, app, _ = system
    _, id = setup(client)
    before = wait_gate(client, app, id)
    jobs = app.state.store.list("job", id)
    client.portal.call(app.state.generation.stop)
    client.portal.call(app.state.generation.start)
    after = client.get(f"{ROOT}/{id}").json()
    assert after == before
    assert not app.state.generation.busy
    assert app.state.store.list("job", id) == jobs
    post(client, id, decision(client, id))
    wait_gate(client, app, id)
