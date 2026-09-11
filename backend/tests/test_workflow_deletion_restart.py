import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.store import Store
from app.main import create_app
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport


def records(store):
    return {kind: store.list(kind) for kind in ("workflow", "settings", "episode", "job", "asset")}


@pytest.mark.parametrize("target", ["default_image", "default_video", "imported"])
def test_deleted_workflows_stay_deleted_after_app_restarts(tmp_path, target):
    config = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(config)
    with TestClient(app) as client:
        for kind in ("image", "video"):
            original = client.get(f"/api/v1/workflows/default_{kind}").json()
            custom = client.post(
                "/api/v1/workflows/import",
                json={
                    "name": f"custom {kind}",
                    "capability": original["capability"],
                    "workflow": original["workflow"],
                },
            ).json()
            assert client.post(f"/api/v1/workflows/{custom['id']}/default").status_code == 200
        if target == "imported":
            imported = client.post(
                "/api/v1/workflows/import",
                json={
                    "name": "首尾帧MiniMax",
                    "capability": original["capability"],
                    "workflow": original["workflow"],
                },
            ).json()
            target = imported["id"]
        episode = client.post(
            "/api/v1/episodes", json={"idea": "Keep my story", "target_duration": 5}
        )
        assert episode.status_code == 201
        before = records(app.state.store)
        assert client.delete(f"/api/v1/workflows/{target}").status_code == 204
        before["workflow"] = [w for w in before["workflow"] if w["id"] != target]
        assert records(app.state.store) == before
    for _ in range(2):
        restarted = create_app(config)
        with TestClient(restarted) as client:
            assert client.get(f"/api/v1/workflows/{target}").status_code == 404
            assert target not in {w["id"] for w in client.get("/api/v1/workflows").json()}
            assert records(restarted.state.store) == before


def test_referenced_import_cannot_be_deleted_and_reports_episode(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(config)
    with TestClient(app) as client:
        original = client.get("/api/v1/workflows/default_video").json()
        custom = client.post(
            "/api/v1/workflows/import",
            json={
                "name": "首尾帧MiniMax",
                "capability": original["capability"],
                "workflow": original["workflow"],
            },
        ).json()
        episode = client.post(
            "/api/v1/episodes",
            json={
                "idea": "My existing film",
                "target_duration": 5,
                "video_workflow_id": custom["id"],
            },
        ).json()
        app.state.store.update("episode", episode["id"], {"title": "三虎闯侏罗纪"})
        before = records(app.state.store)
        result = client.delete(f"/api/v1/workflows/{custom['id']}")
        assert result.status_code == 409
        error = result.json()["error"]
        assert "未删除" in error["message"] and "三虎闯侏罗纪" in error["message"]
        assert error["details"]["episodes"] == [{"id": episode["id"], "title": "三虎闯侏罗纪"}]
        assert records(app.state.store) == before
    restarted = create_app(config)
    with TestClient(restarted):
        assert records(restarted.state.store) == before


def test_legacy_defaults_migrate_without_recreating_deleted_templates(tmp_path):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    for kind in ("image", "video"):
        original = store.get("workflow", f"default_{kind}")
        custom = manager.import_workflow(
            WorkflowImport(
                name=f"custom {kind}",
                capability=original["capability"],
                workflow=original["workflow"],
            )
        )
        manager.set_default(custom["id"])
        manager.delete(original["id"])
    expected = store.get("settings", "settings")["default_capabilities"]
    workflows = store.list("workflow")
    store.update("settings", "settings", lambda s: s.pop("default_capabilities"))
    store.close()
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    assert store.list("workflow") == workflows
    assert store.get("settings", "settings")["default_capabilities"] == expected
    before = records(store)
    manager.bootstrap()
    assert records(store) == before
    store.close()


@pytest.mark.parametrize("defaults", [{}, {"default_image": "missing", "default_video": None}])
def test_existing_empty_library_is_not_reseeded(tmp_path, defaults):
    store = Store(tmp_path)
    store.create("settings", defaults, id="settings")
    manager = WorkflowManager(store)
    manager.bootstrap()
    assert store.list("workflow") == []
    settings = store.get("settings", "settings")
    assert settings["default_capabilities"] == {}
    assert all(settings[key] == value for key, value in defaults.items())
    store.close()


def test_interrupted_first_initialization_completes_without_overwriting(tmp_path, monkeypatch):
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    original_import = manager.import_workflow

    def fail_second_import(request, id=None):
        if id == "default_video":
            raise RuntimeError("startup interrupted")
        return original_import(request, id)

    monkeypatch.setattr(manager, "import_workflow", fail_second_import)
    with pytest.raises(RuntimeError, match="startup interrupted"):
        manager.bootstrap()
    image = store.get("workflow", "default_image")
    store.close()
    store = Store(tmp_path)
    manager = WorkflowManager(store)
    manager.bootstrap()
    assert store.get("workflow", "default_image") == image
    assert {p["id"] for p in store.list("workflow")} == {"default_image", "default_video"}
    assert store.get("settings", "settings")["default_capabilities"] == {
        "TEXT_TO_IMAGE": "default_image",
        "FIRST_LAST_TO_VIDEO": "default_video",
    }
    before = records(store)
    manager.bootstrap()
    assert records(store) == before
    store.close()
