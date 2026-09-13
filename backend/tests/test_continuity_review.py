from copy import deepcopy
from io import BytesIO
from uuid import uuid4

from PIL import Image

from app.media.service import run_process
from tests.test_api_pipeline import wait_episode
from tests.test_creation_mcp import call, connect
from tests.test_creation_packages import BASE, create, submit


def complete_creation(system):
    client, app, _ = system
    project, _ = create(client)
    delivered, _ = submit(client, project)
    eid = delivered["episode_id"]
    preview = client.get(f"/api/v1/episodes/{eid}").json()
    accepted = client.post(
        f"{BASE}/{project}/productions/{eid}/confirm",
        json={"request_id": str(uuid4()), "expected_version": preview["version"], "confirm": True},
    )
    assert accepted.status_code == 202
    final = wait_episode(client, eid)
    assert final["status"] == "COMPLETED", final.get("error")
    return project, final


def test_review_is_read_only_until_recorded_and_stale_after_neighbor_changes(system):
    client, app, _ = system
    _, final = complete_creation(system)
    eid, sid = final["id"], final["shots"][1]["id"]
    url = f"/api/v1/episodes/{eid}/shots/{sid}/continuity-review"
    before = app.state.store.get("episode", eid)
    context = client.get(url).json()
    assert context["frames"][0]["label"] == "前镜实际尾部"
    image = client.get(context["frames"][2]["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert len(image.content) > 100
    assert app.state.store.get("episode", eid) == before
    request = {
        "request_id": str(uuid4()),
        "expected_version": context["version"],
        "review_key": context["review_key"],
        "verdict": "passed",
        "notes": "Synthetic frames checked; this is not model quality acceptance.",
    }
    saved = client.post(url, json=request)
    assert saved.status_code == 200, saved.text
    version = app.state.store.get("episode", eid)["version"]
    assert client.post(url, json=request).json() == saved.json()
    assert app.state.store.get("episode", eid)["version"] == version
    assert client.post(url, json={**request, "notes": "changed"}).status_code == 409
    assert client.get(url).json()["review"]["status"] == "passed"

    def replace(episode):
        episode["shots"][0]["prompts"]["start_frame_prompt"] += " A different framing."

    app.state.store.update("episode", eid, replace)
    assert client.get(url).json()["review"]["status"] == "stale"
    assert client.get(context["frames"][2]["url"]).status_code == 409
    current = app.state.store.get("episode", eid)
    assert (
        client.post(
            url,
            json={**request, "request_id": str(uuid4()), "expected_version": current["version"]},
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/episodes/{eid}").json()["shots"][1]["needs_review"]
    assert (
        client.get(f"/api/v1/episodes/{eid}/shots/not-owned/continuity-review").status_code == 404
    )


def test_mcp_labeled_review_ownership_and_idempotent_local_rerun(system):
    client, app, _ = system
    project, final = complete_creation(system)
    eid, sid = final["id"], final["shots"][1]["id"]
    old_shot = deepcopy(final["shots"][0])

    async def exercise():
        async with connect(app) as session:
            inspected = await session.call_tool(
                "inspect_shot_continuity",
                {"project_id": project, "episode_id": eid, "shot_id": sid},
            )
            assert not inspected.isError
            assert len([item for item in inspected.content if item.type == "image"]) == 10
            context = inspected.structuredContent
            denied = await session.call_tool(
                "inspect_shot_continuity",
                {"project_id": "unrelated", "episode_id": eid, "shot_id": sid},
            )
            assert denied.isError and denied.structuredContent["error"]["code"] == "NOT_FOUND"
            review = {
                "request_id": str(uuid4()),
                "expected_version": context["version"],
                "review_key": context["review_key"],
                "verdict": "needs_changes",
                "notes": "Fixture boundary requires adjustment.",
            }
            saved = await call(
                session,
                "record_shot_continuity_review",
                {"project_id": project, "episode_id": eid, "shot_id": sid, "request": review},
            )
            assert saved["verdict"] == "needs_changes"
            latest = app.state.store.get("episode", eid)
            request = {
                "request_id": str(uuid4()),
                "expected_version": latest["version"],
                "confirm": True,
                "scope": "video",
                "shot_ids": [sid],
                "new_seed": True,
            }
            args = {"project_id": project, "episode_id": eid, "request": request}
            await call(session, "rerun_production_shots", args)
            await call(session, "rerun_production_shots", args)
            changed = await session.call_tool(
                "rerun_production_shots",
                {**args, "request": {**request, "shot_ids": [old_shot["id"]]}},
            )
            assert (
                changed.isError
                and changed.structuredContent["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            )

    client.portal.call(exercise)
    done = wait_episode(client, eid)
    assert done["status"] == "COMPLETED", done.get("error")
    assert len(done["rerun_history"]) == 1
    assert done["shots"][0] == old_shot
    assert done["shots"][1]["video_asset_id"] != final["shots"][1]["video_asset_id"]
    assert done["shots"][1]["continuity_review_status"]["status"] == "stale"
    assert app.state.assets.path(final["final_video_asset_id"]).is_file()


def test_interior_review_exposes_a_cut_when_both_endpoints_match(system):
    client, app, _ = system
    _, final = complete_creation(system)
    eid, sid = final["id"], final["shots"][0]["id"]

    async def make_clip():
        path = app.state.assets.allocate(eid, ".mp4")
        await run_process(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=24:d=3",
            "-vf",
            "drawbox=color=blue:t=fill:enable='between(t,1,2)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        )
        return await app.state.assets.register(path, eid, "SHOT_VIDEO", sid)

    asset = client.portal.call(make_clip)

    def replace(episode):
        episode["shots"][0]["video_asset_id"] = asset["id"]

    app.state.store.update("episode", eid, replace)
    context = client.get(f"/api/v1/episodes/{eid}/shots/{sid}/continuity-review").json()
    observed = {}
    for frame in context["frames"]:
        if frame["fraction"] in (0, 0.5, 1):
            response = client.get(frame["url"])
            assert response.status_code == 200
            observed[frame["fraction"]] = (
                Image.open(BytesIO(response.content)).convert("RGB").getpixel((20, 20))
            )
    for endpoint in (0, 1):
        red, _, blue = observed[endpoint]
        assert red > 200 and blue < 30
    red, _, blue = observed[0.5]
    assert blue > 200 and red < 30
