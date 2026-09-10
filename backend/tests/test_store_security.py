import pytest

from app.core.errors import AppError
from app.core.security import safe_path, validate_comfy_url
from app.db.store import Store


def test_store_persists_and_does_not_leak_mutable_references(tmp_path):
    store = Store(tmp_path)
    episode = store.create("episode", {"shots": [{"status": "PENDING"}]})
    episode["shots"][0]["status"] = "BAD"
    assert store.get("episode", episode["id"])["shots"][0]["status"] == "PENDING"
    store.update("episode", episode["id"], {"status": "COMPLETED"})
    store.close()
    reopened = Store(tmp_path)
    assert reopened.get("episode", episode["id"])["version"] == 2
    assert reopened.get("episode", episode["id"])["status"] == "COMPLETED"
    reopened.close()


async def test_comfy_url_blocks_public_and_metadata():
    assert await validate_comfy_url("http://127.0.0.1:8188/") == "http://127.0.0.1:8188"
    for url in [
        "http://8.8.8.8",
        "http://169.254.169.254",
        "file:///tmp/a",
        "http://x:y@localhost",
    ]:
        with pytest.raises(AppError):
            await validate_comfy_url(url)


def test_asset_path_cannot_escape(tmp_path):
    with pytest.raises(AppError):
        safe_path(tmp_path, "../secrets")
    assert safe_path(tmp_path, "episode/asset.png") == tmp_path / "episode/asset.png"
