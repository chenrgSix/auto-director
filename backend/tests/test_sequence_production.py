from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.workflows.schema import WorkflowImport
from tests.sequence_fakes import SequenceComfy, SequenceProvider, native_prompt
from tests.test_api_pipeline import wait_episode
from tests.test_creation_packages import create, document, submit

FOLDER = Path(__file__).resolve().parents[2] / "workflow_examples/codex_h3_continuous"


@pytest.fixture
def sequence_system(tmp_path):
    config = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_model="fixture",
        vlm_model="",
        poll_interval=0.01,
        render_timeout=20,
    )
    comfy = SequenceComfy(config, b"")
    app = create_app(config, client_factory=comfy.client, provider_factory=SequenceProvider)
    with TestClient(app) as client:
        request = WorkflowImport.model_validate_json(
            (FOLDER / "continuous.profile.json").read_text()
        )
        profile = app.state.workflows.import_workflow(request)
        comfy.install(profile)
        yield client, app, comfy, profile


def package(profile, *, review=False):
    result = document()
    result["brief"].update(video_workflow_id=profile["id"], image_review_required=review)
    for shot in result["shots"]:
        prompts = shot["prompts"]
        for field in ("image_prompt", "start_frame_prompt", "end_frame_prompt"):
            prompts.pop(field)
        prompts["video_prompt"] = native_prompt("The lion walks steadily through the forest.")
        prompts["visual_continuity"] = {
            "scene_id": "forest",
            "visible_character_ids": ["lion"],
            "reference_roles": ["character:lion", "environment"],
            "framing": "wide",
            "state_in": {"direction": "right"},
            "state_out": {"direction": "right"},
            "intentional_jump": "",
        }
    return result


def prepare(system, *, review=False):
    client, app, comfy, profile = system
    project, _ = create(client, package(profile, review=review))
    delivery, _ = submit(client, project)
    episode = client.get(f"/api/v1/episodes/{delivery['episode_id']}").json()
    return project, episode


def approve(client, episode):
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/approve", json={"expected_version": episode["version"]}
    )
    assert response.status_code == 202, response.text


def test_creation_preview_requires_no_endpoint_prompts_or_image_workflow(sequence_system):
    client, app, comfy, profile = sequence_system
    app.state.store.update(
        "settings", "settings", {"default_image": "", "default_capabilities": {}}
    )
    # Explicit reference selection is enough; the unrelated image default may be absent.
    data = package(profile)
    data["brief"]["reference_workflow_id"] = next(
        p["id"] for p in app.state.store.list("workflow") if p["capability"] == "TEXT_TO_IMAGE"
    )
    project, _ = create(client, data)
    context = client.get(f"/api/v1/creation/projects/{project}/context").json()
    assert context["constraints"]["sequence_limits"]["keyframes_required"] is False
    delivery, _ = submit(client, project)
    episode = client.get(f"/api/v1/episodes/{delivery['episode_id']}").json()
    assert episode["production_mode"] == "reference_sequence"
    assert len(episode["sequence_groups"]) == 1
    assert all(not s["prompts"]["start_frame_prompt"] for s in episode["shots"])
    assert not [call for call in comfy.calls if call[1] == "/prompt"]


def test_complete_group_binds_distinct_full_segments_and_reruns_whole_group(sequence_system):
    client, app, comfy, profile = sequence_system
    _, episode = prepare(sequence_system)
    approve(client, episode)
    result = wait_episode(client, episode["id"])
    assert result["status"] == "COMPLETED", result.get("error")
    shots = result["shots"]
    assert len({s["video_asset_id"] for s in shots}) == 2
    assert all(not s["start_frame_asset_id"] and not s["end_frame_asset_id"] for s in shots)
    assert all(s["actual_end_frame_asset_id"] for s in shots)
    jobs = app.state.store.list("job", episode["id"])
    groups = [job for job in jobs if job["type"] == "SEQUENCE_VIDEO"]
    assert len(groups) == 1 and groups[0]["sequence_output"]["segment_frames"] == [73, 85]
    assert not any(job["type"] in {"SHOT_START_FRAME", "SHOT_END_FRAME"} for job in jobs)
    assert abs(result["final_duration"] - 158 / 24) < 0.05
    before = [s["video_asset_id"] for s in shots]
    rerun = client.post(
        f"/api/v1/episodes/{episode['id']}/rerun",
        json={
            "expected_version": result["version"],
            "scope": "video",
            "shot_ids": [shots[1]["id"]],
        },
    )
    assert rerun.status_code == 202, rerun.text
    assert set(rerun.json()["rerun_history"][-1]["affected_shot_ids"]) == {s["id"] for s in shots}
    after = wait_episode(client, episode["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert [s["video_asset_id"] for s in after["shots"]] != before
    assert all(app.state.generation.assets.path(id).exists() for id in before)


@pytest.mark.parametrize("defect", ["missing_refs", "malformed_report"])
def test_unverified_outputs_never_bind_partial_group(sequence_system, defect):
    client, app, comfy, profile = sequence_system
    setattr(comfy, defect, True)
    _, episode = prepare(sequence_system)
    approve(client, episode)
    result = wait_episode(client, episode["id"])
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "SEQUENCE_REPORT_INVALID"
    assert all(not shot["video_asset_id"] for shot in result["shots"])
    job = next(
        j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SEQUENCE_VIDEO"
    )
    assert job["source_output_asset_ids"] and job["comfy_prompt_id"]


def test_legacy_mode_still_rejects_missing_endpoints(sequence_system):
    client, app, comfy, profile = sequence_system
    data = package(profile)
    data["brief"].pop("video_workflow_id")
    project, _ = create(client, data)
    report = client.post(f"/api/v1/creation/projects/{project}/validate").json()
    assert not report["valid"]


def test_group_rejects_more_than_nine_references(sequence_system):
    from app.generation.sequence import validate_groups

    data = package(sequence_system[3])
    data["shots"][0]["prompts"]["visual_continuity"]["reference_roles"] = ["environment"] * 10
    with pytest.raises(AppError):
        validate_groups({"bible": data["bible"], "shots": data["shots"]})


def test_reference_approval_via_mcp_skips_keyframe_gate_and_reports_group(sequence_system):
    from uuid import uuid4

    from tests.test_creation_mcp import call, connect
    from tests.test_preproduction_review import wait_gate

    client, app, comfy, _ = sequence_system
    project, episode = prepare(sequence_system, review=True)
    approve(client, episode)
    waiting = wait_gate(client, app, episode["id"])
    assert waiting["preproduction_pending"]["stage"] == "references"
    assert not any(
        j["type"] == "SEQUENCE_VIDEO" for j in app.state.store.list("job", episode["id"])
    )

    context = client.get(f"/api/v1/episodes/{episode['id']}/image-review").json()

    async def confirm():
        async with connect(app) as session:
            inspected = await session.call_tool(
                "inspect_preproduction_images", {"project_id": project, "episode_id": episode["id"]}
            )
            assert not inspected.isError and any(c.type == "image" for c in inspected.content)
            return await call(
                session,
                "decide_preproduction_images",
                {
                    "project_id": project,
                    "episode_id": episode["id"],
                    "request": {
                        "request_id": str(uuid4()),
                        "expected_version": context["version"],
                        "review_key": context["review_key"],
                        "decision": "approve",
                        "notes": "Synthetic reference review; not visual quality evidence.",
                    },
                },
            )

    client.portal.call(confirm)
    result = wait_gate(client, app, episode["id"], completed=True)
    assert result["metrics"]["llm_calls"] == 0

    async def feedback():
        async with connect(app) as session:
            result = await call(
                session,
                "get_production_feedback",
                {"project_id": project, "episode_id": episode["id"]},
            )
            assert result["production_mode"] == "reference_sequence"
            assert len(result["sequence_groups"][0]["shot_ids"]) == 2
            request = {
                "expected_version": result["version"],
                "request_id": str(uuid4()),
                "confirm": True,
                "scope": "video",
                "shot_ids": [result["shots"][1]["id"]],
            }
            rerun = await call(
                session,
                "rerun_production_shots",
                {"project_id": project, "episode_id": episode["id"], "request": request},
            )
            assert rerun["rerun_scope"] == "whole_group" and len(rerun["affected_shot_ids"]) == 2

    client.portal.call(feedback)
    assert wait_episode(client, episode["id"])["status"] == "COMPLETED"


def test_unknown_group_resumes_original_prompt_without_resubmitting(sequence_system):
    client, app, comfy, _ = sequence_system
    _, episode = prepare(sequence_system)
    original_factory = app.state.generation.engine.client_factory
    interrupted = False

    def factory(url):
        remote = original_factory(url)
        original_execute = remote.execute

        async def execute(graph, job_id, on_submit, *args, **kwargs):
            async def submitted(data):
                nonlocal interrupted
                await on_submit(data)
                if not interrupted and any(
                    n["class_type"] == "MiniMaxH3Director" for n in graph.values()
                ):
                    interrupted = True
                    raise AppError("JOB_TIMEOUT", "Fixture interrupted after accepted submission")

            return await original_execute(graph, job_id, submitted, *args, **kwargs)

        remote.execute = execute
        return remote

    app.state.generation.engine.client_factory = factory
    approve(client, episode)
    failed = wait_episode(client, episode["id"])
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "JOB_TIMEOUT"
    pending = app.state.generation.engine.unresolved()
    assert len(pending) == 1 and pending[0]["comfy_prompt_id"]
    before = len(comfy.prompts)
    response = client.post(f"/api/v1/episodes/{episode['id']}/generate")
    assert response.status_code == 202, response.text
    result = wait_episode(client, episode["id"])
    assert result["status"] == "COMPLETED", result.get("error")
    assert len(comfy.prompts) == before
    assert not app.state.generation.engine.unresolved()


def test_api_creative_path_produces_no_endpoint_prompts(sequence_system):
    client, app, comfy, profile = sequence_system
    response = client.post(
        "/api/v1/episodes",
        json={
            "idea": "A lion walks without stopping",
            "target_duration": 10,
            "quality": "fast",
            "preview_required": True,
            "qa_enabled": False,
            "video_workflow_id": profile["id"],
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    queued = client.post(f"/api/v1/episodes/{id}/preview")
    assert queued.status_code == 202, queued.text
    import time

    for _ in range(200):
        result = client.get(f"/api/v1/episodes/{id}").json()
        if (
            result["status"] in {"AWAITING_REVIEW", "FAILED"}
            and id not in app.state.generation.busy
        ):
            break
        time.sleep(0.01)
    assert result["status"] == "AWAITING_REVIEW", result.get("error")
    assert all(not shot["prompts"]["start_frame_prompt"] for shot in result["shots"])
    assert len(result["sequence_groups"]) == 1
    assert not comfy.prompts


def test_timeline_membership_change_invalidates_whole_group(sequence_system):
    client, app, _, _ = sequence_system
    _, episode = prepare(sequence_system)
    approve(client, episode)
    result = wait_episode(client, episode["id"])
    old = result["shots"][0]["video_asset_id"]
    response = client.patch(
        f"/api/v1/episodes/{episode['id']}/timeline",
        json={"shots": [{"id": s["id"], "enabled": i == 0} for i, s in enumerate(result["shots"])]},
    )
    assert response.status_code == 200, response.text
    assert not response.json()["shots"][0]["video_asset_id"]
    assert app.state.generation.assets.path(old).exists()


def test_preview_can_split_and_rejoin_sequence_group(sequence_system):
    client, app, _, profile = sequence_system
    _, episode = prepare(sequence_system)
    edits = [
        {
            "id": s["id"],
            "title": s["title"],
            "duration": s["duration"],
            "video_prompt": s["prompts"]["video_prompt"],
            "transition_from_previous": "CUT",
        }
        for s in episode["shots"]
    ]
    response = client.patch(
        f"/api/v1/episodes/{episode['id']}/preview",
        json={"expected_version": episode["version"], "shots": edits},
    )
    assert response.status_code == 200, response.text
    current = client.get(f"/api/v1/episodes/{episode['id']}").json()
    assert len(current["sequence_groups"]) == 2
    edits[1]["transition_from_previous"] = "CONTINUE_VIDEO"
    response = client.patch(
        f"/api/v1/episodes/{episode['id']}/preview",
        json={"expected_version": current["version"], "shots": edits},
    )
    assert response.status_code == 200, response.text
    assert len(client.get(f"/api/v1/episodes/{episode['id']}").json()["sequence_groups"]) == 1
    response = client.patch(
        f"/api/v1/episodes/{episode['id']}/workflows",
        json={
            "expected_version": response.json()["version"],
            "video_workflow_id": profile["id"],
            "reference_workflow_id": episode["reference_workflow_id"],
        },
    )
    assert response.status_code == 200, response.text


def test_group_cancel_after_submit_does_not_bind_late_media(sequence_system):
    import asyncio
    import threading

    from tests.test_cancellation import wait_idle

    client, app, comfy, _ = sequence_system
    _, episode = prepare(sequence_system)
    entered = threading.Event()
    release = asyncio.Event()
    original_factory = app.state.engine.client_factory

    def factory(url):
        remote = original_factory(url)
        original_execute = remote.execute

        async def execute(graph, job_id, on_submit, *args, **kwargs):
            async def submitted(data):
                await on_submit(data)
                if any(n["class_type"] == "MiniMaxH3Director" for n in graph.values()):
                    entered.set()
                    await release.wait()
                    raise AppError("CANCELLED", "Fixture acknowledged cancellation")

            return await original_execute(graph, job_id, submitted, *args, **kwargs)

        remote.execute = execute
        return remote

    app.state.engine.client_factory = factory
    approve(client, episode)
    assert entered.wait(5)
    assert client.post(f"/api/v1/episodes/{episode['id']}/cancel").json()["status"] == "CANCELLED"
    client.portal.call(release.set)
    wait_idle(app.state.generation, episode["id"])
    result = client.get(f"/api/v1/episodes/{episode['id']}").json()
    assert not any(s["video_asset_id"] for s in result["shots"])
    assert not result["final_video_asset_id"]
    assert (
        next(
            j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SEQUENCE_VIDEO"
        )["status"]
        == "CANCELLED"
    )


def test_failed_report_continuation_does_not_resubmit_remote_group(sequence_system):
    client, app, comfy, _ = sequence_system
    comfy.malformed_report = True
    _, episode = prepare(sequence_system)
    approve(client, episode)
    assert wait_episode(client, episode["id"])["status"] == "FAILED"
    before = len(comfy.prompts)
    assert client.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 202
    assert wait_episode(client, episode["id"])["status"] == "FAILED"
    assert len(comfy.prompts) == before


def test_new_app_start_recovers_group_from_persisted_prompt(sequence_system):
    client, app, comfy, _ = sequence_system
    _, episode = prepare(sequence_system)
    approve(client, episode)
    completed = wait_episode(client, episode["id"])
    assert completed["status"] == "COMPLETED"
    job = next(
        j for j in app.state.store.list("job", episode["id"]) if j["type"] == "SEQUENCE_VIDEO"
    )
    # Model a crash after remote acceptance, before any output is bound locally.
    client.portal.call(app.state.generation.stop)
    app.state.store.update("job", job["id"], {"status": "RUNNING", "output_asset_ids": []})
    for shot in completed["shots"]:
        shot.update(status="RENDERING_VIDEO", video_asset_id=None)
    app.state.store.update(
        "episode",
        episode["id"],
        {"status": "RENDERING_VIDEO", "shots": completed["shots"], "final_video_asset_id": None},
    )
    before = len(comfy.prompts)
    restarted = create_app(
        app.state.config, client_factory=comfy.client, provider_factory=SequenceProvider
    )
    with TestClient(restarted) as recovered:
        original = restarted.state.engine.unresolved()
        assert len(original) == 1 and original[0]["comfy_prompt_id"] == job["comfy_prompt_id"]
        assert recovered.post(f"/api/v1/episodes/{episode['id']}/generate").status_code == 202
        final = wait_episode(recovered, episode["id"])
        assert final["status"] == "COMPLETED", final.get("error")
        assert len(comfy.prompts) == before
        assert len({s["video_asset_id"] for s in final["shots"]}) == 2
