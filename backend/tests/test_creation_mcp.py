import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.test_api_pipeline import wait_episode
from tests.test_creation_packages import BASE, create, document, submit


@asynccontextmanager
async def connect(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        async with streamable_http_client("http://testserver/mcp/", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                assert "AutoDirector" == initialized.serverInfo.name
                yield session


async def call(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    assert not result.isError, result
    return result.structuredContent


def test_official_client_discovers_tools_and_roundtrips_web_changes(system):
    client, app, comfy = system

    async def exercise():
        async with connect(app) as session:
            tools = await session.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert len(by_name) == 12
            assert by_name["get_creation_context"].annotations.readOnlyHint
            assert not by_name["confirm_production"].annotations.readOnlyHint
            created = await call(
                session,
                "create_creation_project",
                {
                    "request": {"request_id": str(uuid4()), "document": document()},
                },
            )
            project = created["project_id"]
            context = await call(session, "get_creation_context", {"project_id": project})
            assert context["constraints_hash"] and context["constraints"]["shot_prompts_schema"]
            saved = await call(
                session,
                "save_creation_package",
                {
                    "project_id": project,
                    "request": {
                        "request_id": str(uuid4()),
                        "expected_revision": 1,
                        "document": {**context["document"], "notes": "用户决定保留森林场景"},
                    },
                },
            )
            assert saved["revision"] == 2
            stale = await session.call_tool(
                "save_creation_package",
                {
                    "project_id": project,
                    "request": {
                        "request_id": str(uuid4()),
                        "expected_revision": 1,
                        "document": context["document"],
                    },
                },
            )
            assert stale.isError and stale.structuredContent["error"]["code"] == "CONFLICT"
            return project

    project = client.portal.call(exercise)
    assert client.get(f"{BASE}/{project}").json()["revision"] == 2
    assert client.get(f"{BASE}/{project}/export").json()["notes"] == "用户决定保留森林场景"
    assert not comfy.calls


def test_disconnect_does_not_cancel_accepted_production_and_media_is_readable(system):
    client, app, _ = system

    def forbidden():
        raise AssertionError("MCP package production must not call models")

    app.state.generation.provider_factory = forbidden
    gate = asyncio.Event()
    original = app.state.generation.generate

    async def delayed(*args, **kwargs):
        await gate.wait()
        await original(*args, **kwargs)

    app.state.generation.generate = delayed

    async def start():
        async with connect(app) as session:
            project = (
                await call(
                    session,
                    "create_creation_project",
                    {
                        "request": {"request_id": str(uuid4()), "document": document()},
                    },
                )
            )["project_id"]
            report = await call(session, "validate_creation_package", {"project_id": project})
            assert report["valid"]
            request = {
                "request_id": str(uuid4()),
                "expected_revision": 1,
                "constraints_hash": report["constraints_hash"],
            }
            delivery = await call(
                session, "submit_creation_package", {"project_id": project, "request": request}
            )
            repeated = await call(
                session, "submit_creation_package", {"project_id": project, "request": request}
            )
            assert delivery == repeated
            approval = {
                "request_id": str(uuid4()),
                "expected_version": delivery["version"],
                "confirm": True,
            }
            arguments = {
                "project_id": project,
                "episode_id": delivery["episode_id"],
                "request": approval,
            }
            await call(session, "confirm_production", arguments)
            return project, delivery["episode_id"], arguments

    project, id, approval = client.portal.call(start)
    # The original MCP connection has closed; a separate HTTP request and editor still work.
    assert client.get("/api/v1/health").status_code == 200
    other, _ = create(client)
    assert other != project
    assert app.state.store.get("episode", id)["status"] == "QUEUED"
    client.portal.call(gate.set)
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final["error"]
    assert final["metrics"]["llm_calls"] == 0

    async def reconnect():
        async with connect(app) as session:
            replayed = await call(session, "confirm_production", approval)
            assert replayed["status"] == "COMPLETED"
            feedback = await call(
                session, "get_production_feedback", {"project_id": project, "episode_id": id}
            )
            image_id = final["shots"][0]["start_frame_asset_id"]
            image = await session.call_tool(
                "inspect_production_media",
                {
                    "project_id": project,
                    "episode_id": id,
                    "asset_id": image_id,
                },
            )
            assert not image.isError and any(c.type == "image" for c in image.content)
            video = await session.call_tool(
                "inspect_production_media",
                {
                    "project_id": project,
                    "episode_id": id,
                    "asset_id": feedback["final_video_asset_id"],
                },
            )
            assert not video.isError
            assert len([c for c in video.content if c.type == "image"]) == 5
            denied = await session.call_tool(
                "get_production_feedback", {"project_id": other, "episode_id": id}
            )
            assert denied.isError and denied.structuredContent["error"]["code"] == "NOT_FOUND"

    client.portal.call(reconnect)


def test_mcp_requires_explicit_confirmation_and_rejects_browser_origin(system):
    client, app, comfy = system
    project, _ = create(client)
    delivery, _ = submit(client, project)

    async def exercise():
        async with connect(app) as session:
            denied = await session.call_tool(
                "confirm_production",
                {
                    "project_id": project,
                    "episode_id": delivery["episode_id"],
                    "request": {
                        "request_id": str(uuid4()),
                        "expected_version": 1,
                        "confirm": False,
                    },
                },
            )
            assert denied.isError
            cancelled = await call(
                session,
                "cancel_production",
                {
                    "project_id": project,
                    "episode_id": delivery["episode_id"],
                },
            )
            # No production was authorized, so cancelling is an idempotent no-op.
            assert cancelled["status"] == "AWAITING_REVIEW"

    client.portal.call(exercise)
    assert not comfy.prompts
    response = client.post("/mcp/", headers={"Origin": "https://untrusted.example"}, json={})
    assert response.status_code == 403
    assert client.get("/mcp/", headers={"Host": "untrusted.example"}).status_code == 400
