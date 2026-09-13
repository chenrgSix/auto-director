from copy import deepcopy
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.creation.schemas import CreationPackage
from app.db.store import Store
from tests.test_api_pipeline import wait_episode
from tests.test_episode_workflows import imported

BASE = "/api/v1/creation/projects"


def document():
    return {
        "brief": {"idea": "一只狮子走过森林", "target_duration": 5, "quality": "fast"},
        "title": "森林来客",
        "logline": "狮子走向林间空地。",
        "bible": {
            "characters": [
                {
                    "id": "lion",
                    "description": "golden lion",
                    "distinguishing_features": ["dark mane"],
                }
            ],
            "environment": {"place": "forest"},
            "style": {"look": "documentary"},
            "continuity_rules": ["One lion"],
            "negative_prompt": "artifacts",
            "camera_motion": "static",
            "motion_strength": 0.2,
        },
        "shots": [
            {
                "id": f"shot_{i}",
                "index": i,
                "title": f"镜头 {i + 1}",
                "duration": 2.5,
                "purpose": "explore",
                "action": "walk",
                "camera": "wide",
                "start_state": "start",
                "end_state": "end",
                "transition_from_previous": "CUT" if i == 0 else "CONTINUE_FRAME",
                "prompts": {
                    "image_prompt": "A lion",
                    "start_frame_prompt": "Lion starts walking",
                    "end_frame_prompt": "Lion has walked forward",
                    "video_prompt": "Lion walking",
                    "negative_prompt": "artifacts",
                    "camera_motion": "slow tracking",
                    "motion_strength": 0.6,
                    "continuity_state": {"direction": "right"},
                },
            }
            for i in range(2)
        ],
    }


def create(client, package=None):
    body = {"request_id": str(uuid4()), "document": package or document()}
    result = client.post(BASE, json=body)
    assert result.status_code == 201, result.text
    return result.json()["project_id"], body


def submit(client, project):
    report = client.post(f"{BASE}/{project}/validate").json()
    assert report["valid"], report
    body = {
        "request_id": str(uuid4()),
        "expected_revision": report["revision"],
        "constraints_hash": report["constraints_hash"],
    }
    response = client.post(f"{BASE}/{project}/submit", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def test_versions_roundtrip_cas_and_idempotent_writes(system):
    client, app, comfy = system
    project, body = create(client)
    assert client.post(BASE, json=body).json()["project_id"] == project
    exported = client.get(f"{BASE}/{project}/export").json()
    assert CreationPackage.model_validate(exported).model_dump(mode="json") == exported
    change = {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "document": {**exported, "title": "第二版"},
    }
    saved = client.post(f"{BASE}/{project}/revisions", json=change)
    assert saved.status_code == 200, saved.text
    assert client.post(f"{BASE}/{project}/revisions", json=change).json() == saved.json()
    stale = {**change, "request_id": str(uuid4()), "document": exported}
    assert client.post(f"{BASE}/{project}/revisions", json=stale).status_code == 409
    assert (
        client.post(
            f"{BASE}/{project}/revisions", json={**change, "document": exported}
        ).status_code
        == 409
    )
    assert client.get(f"{BASE}/{project}/export?revision=1").json() == exported
    assert client.get(f"{BASE}/{project}/export").json()["title"] == "第二版"
    assert len(app.state.store.list("creation_revision", project)) == 2
    assert not comfy.calls and not app.state.store.list("episode")


@pytest.mark.parametrize(
    "case",
    ["draft", "total", "duplicate", "order", "first", "characters", "precision", "ai", "shot_ai"],
)
def test_invalid_delivery_keeps_draft_and_creates_nothing(system, case):
    client, app, _ = system
    package = document()
    if case == "draft":
        package["shots"][0]["prompts"] = None
    if case == "total":
        package["shots"][0]["duration"] = 2
    if case == "duplicate":
        package["shots"][1]["id"] = package["shots"][0]["id"]
    if case == "order":
        package["shots"][0]["index"] = 1
    if case == "first":
        package["shots"][0]["transition_from_previous"] = "CONTINUE_FRAME"
    if case == "characters":
        package["bible"]["characters"] *= 2
    if case == "precision":
        package["shots"][0]["duration"] = 2.501
    if case == "ai":
        package["bible"]["ai_parameters"] = {"invented": {"seed": 1}}
    if case == "shot_ai":
        package["shots"][0]["prompts"]["ai_parameters"] = {"invented": {"seed": 1}}
    project, _ = create(client, package)
    before = app.state.creation.detail(project)
    report = client.post(f"{BASE}/{project}/validate").json()
    assert not report["valid"] and report["issues"]
    context = client.get(f"{BASE}/{project}/context").json()
    response = client.post(
        f"{BASE}/{project}/submit",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "constraints_hash": context["constraints_hash"],
        },
    )
    assert response.status_code == 422, response.text
    assert app.state.creation.detail(project) == before
    assert not app.state.store.list("episode")


@pytest.mark.parametrize("i2v", [False, True])
def test_full_package_production_never_calls_text_model_and_replays_requests(system, i2v):
    client, app, comfy = system

    def forbidden_provider():
        raise AssertionError("Package production must not construct a model provider")

    app.state.generation.provider_factory = forbidden_provider
    app.state.config.llm_model = ""
    package = document()
    if i2v:
        package["brief"]["video_workflow_id"] = imported(app, "IMAGE_TO_VIDEO")["id"]
    project, _ = create(client, package)
    delivery, request = submit(client, project)
    id = delivery["episode_id"]
    assert client.post(f"{BASE}/{project}/submit", json=request).json() == delivery
    assert len(app.state.store.list("episode")) == 1
    episode = client.get(f"/api/v1/episodes/{id}").json()
    assert episode["status"] == "AWAITING_REVIEW" and not comfy.calls
    assert not episode.get("script_review")
    assert client.post(f"/api/v1/episodes/{id}/generate").status_code == 409
    assert client.post(f"/api/v1/episodes/{id}/preview").status_code == 409
    approval = {"request_id": str(uuid4()), "expected_version": episode["version"], "confirm": True}
    path = f"{BASE}/{project}/productions/{id}/confirm"
    assert client.post(path, json=approval).status_code == 202
    assert client.post(path, json=approval).status_code == 202
    final = wait_episode(client, id)
    assert final["status"] == "COMPLETED", final["error"]
    count = len(comfy.prompts)
    assert client.post(path, json=approval).status_code == 202
    assert len(comfy.prompts) == count
    assert final["metrics"]["llm_calls"] == 0
    assert abs(final["final_duration"] - 5) < 0.1
    assert all(s["needs_review"] for s in final["shots"])
    feedback = client.get(f"{BASE}/{project}/productions/{id}").json()
    assert feedback["assets"] and "path" not in feedback["assets"][0]
    assert all(s["creation_shot_id"].startswith("shot_") for s in feedback["shots"])
    optimization = client.post(
        f"/api/v1/episodes/{id}/shots/{final['shots'][0]['id']}/prompt-optimization",
        json={"expected_version": final["version"], "feedback": "修改动作"},
    )
    assert optimization.status_code == 409, optimization.text
    assert optimization.json()["error"]["code"] == "CREATION_PACKAGE_REQUIRED"
    assert (
        client.post(
            f"/api/v1/episodes/{id}/rerun",
            json={"expected_version": final["version"], "scope": "prompts"},
        ).status_code
        == 409
    )


def test_workflow_drift_and_new_revision_leave_delivery_immutable(system):
    client, app, _ = system
    project, _ = create(client)
    delivery, request = submit(client, project)
    before = app.state.store.get("episode", delivery["episode_id"])
    profile_id = before["video_workflow_id"]
    profile = app.state.store.get("workflow", profile_id)
    app.state.store.update(
        "workflow", profile_id, {"configuration_version": profile["version"] + 1}
    )
    assert (
        client.post(
            f"{BASE}/{project}/submit", json={**request, "request_id": str(uuid4())}
        ).status_code
        == 409
    )
    assert client.post(f"{BASE}/{project}/submit", json=request).json() == delivery
    updated = deepcopy(document())
    updated["title"] = "新的创作"
    assert (
        client.post(
            f"{BASE}/{project}/revisions",
            json={"expected_revision": 1, "request_id": str(uuid4()), "document": updated},
        ).status_code
        == 200
    )
    assert app.state.store.get("episode", delivery["episode_id"]) == before
    assert (
        client.post(
            f"{BASE}/{project}/submit", json={**request, "request_id": str(uuid4())}
        ).status_code
        == 409
    )


def test_import_normalizes_missing_defaults_and_rejects_invalid_limits(system):
    client, _, _ = system
    path = "/api/v1/creation/normalize"
    normalized = client.post(path, json={"brief": {"idea": "草稿", "target_duration": 5}})
    assert normalized.status_code == 200
    assert normalized.json()["shots"] == [] and normalized.json()["decisions"] == []
    assert normalized.json()["brief"]["visual_review"] == "manual"
    for value in (None, {"brief": {"idea": "草稿", "target_duration": 5, "max_shot_duration": 31}}):
        assert client.post(path, json=value).status_code == 422


def test_explicit_model_review_requires_a_configured_visual_model(system):
    client, app, _ = system
    app.state.config.vlm_model = ""
    package = document()
    package["brief"]["visual_review"] = "model"
    project, _ = create(client, package)
    report = client.post(f"{BASE}/{project}/validate").json()
    assert not report["valid"]
    assert report["issues"][0]["code"] == "CONFIGURATION_REQUIRED"
    app.state.config.vlm_model = "TEST-VISION"
    delivery, _ = submit(client, project)
    app.state.config.vlm_model = ""
    approval = client.post(
        f"{BASE}/{project}/productions/{delivery['episode_id']}/confirm",
        json={"expected_version": delivery["version"], "request_id": str(uuid4()), "confirm": True},
    )
    assert approval.status_code == 409
    assert approval.json()["error"]["code"] == "CONFIGURATION_REQUIRED"


def test_external_audio_contract_is_validated_on_delivery_and_preview_confirmation(system):
    from tests.test_episode_preview import edits
    from tests.test_native_audio_prompts import H3, LINE, PROMPT

    client, app, comfy = system
    profile = app.state.store.get("workflow", "default_video")
    app.state.store.update(
        "workflow",
        "default_video",
        {"capabilities": {**profile["capabilities"], "audio_prompt_format": H3}},
    )
    package = document()
    project, _ = create(client, package)
    assert not client.post(f"{BASE}/{project}/validate").json()["valid"]
    for shot in package["shots"]:
        shot["prompts"].update(video_prompt=PROMPT, narration_text=LINE)
    saved = client.post(
        f"{BASE}/{project}/revisions",
        json={"expected_revision": 1, "request_id": str(uuid4()), "document": package},
    )
    assert saved.status_code == 200
    delivery, _ = submit(client, project)
    id = delivery["episode_id"]
    episode = client.get(f"/api/v1/episodes/{id}").json()
    changes = edits(episode)
    changes["shots"][0]["video_prompt"] = "Lion walking"
    updated = client.patch(
        f"/api/v1/episodes/{id}/preview",
        json=changes,
    )
    # Existing preview validation may reject the edit immediately; otherwise approval must.
    if updated.status_code == 200:
        approved = client.post(
            f"{BASE}/{project}/productions/{id}/confirm",
            json={
                "expected_version": updated.json()["version"],
                "request_id": str(uuid4()),
                "confirm": True,
            },
        )
        assert approved.status_code == 422, approved.text
    else:
        assert updated.status_code == 422, updated.text
    assert not comfy.prompts


def test_atomic_record_write_rolls_back_and_survives_restart(tmp_path):
    store = Store(tmp_path)

    def broken(tx):
        tx.create("creation_project", {"revision": 1}, id="draft")
        tx.create("creation_revision", {"text": "draft"}, id="revision")
        raise AppError("FAIL", "rollback")

    with pytest.raises(AppError):
        store.atomic(broken)
    assert not store.list("creation_project") and not store.list("creation_revision")
    store.atomic(lambda tx: tx.create("creation_project", {"revision": 1}, id="saved"))
    store.close()
    reopened = Store(tmp_path)
    try:
        assert reopened.get("creation_project", "saved")["revision"] == 1
    finally:
        reopened.close()
