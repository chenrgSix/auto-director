"""Keep the optional, explicitly imported workflow pack usable by AutoDirector."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.db.store import Store
from app.generation.parameters import resolve_parameters
from app.workflows.analyzer import patch, validate_bindings
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowImport

PACK = Path(__file__).resolve().parents[2] / "workflow_examples" / "codex_8gb"
PROFILES = sorted(PACK.glob("*.profile.json"))


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_codex_pack_import_patch_and_user_override(tmp_path, path):
    request = WorkflowImport.model_validate_json(path.read_text())
    assert request.name.startswith("codex-")
    assert request.workflow == json.loads(
        path.with_name(path.name.replace(".profile.", ".api.")).read_text()
    )
    store = Store(tmp_path / "workflows.sqlite3")
    profile = WorkflowManager(store).import_workflow(request)
    before = deepcopy(profile)
    assert not validate_bindings(profile)
    roles = {p["role"]: p for p in profile["parameters"] if p["role"]}
    assert roles["prompt"]["owner"] == "ai"
    assert roles["width"]["owner"] == roles["height"]["owner"] == "system"
    assets = {
        role: f"uploaded/{role}.png"
        for role in ("reference_image", "start_frame", "end_frame")
        if role in roles
    }
    assert all(roles[role]["owner"] == "asset_resolver" for role in assets)
    automatic = {"prompt": "AI scene", "width": 640, "height": 384, "seed": 42}
    if profile["media_type"] == "video":
        assert roles["duration"]["owner"] == "director"
        automatic.update(duration=5, fps=16)
    values, resolved_assets, raw, sources = resolve_parameters(
        profile, automatic, assets, {roles["prompt"]["key"]: "User scene"}, True, None
    )
    graph = patch(profile, {**values, **resolved_assets}, raw)
    for role, value in {"prompt": "User scene", "width": 640, "height": 384, **assets}.items():
        item = roles[role]
        assert graph[item["node_id"]]["inputs"][item["field"]] == value
    assert sources[roles["prompt"]["key"]] == "user"
    assert profile == before
    assert profile["capabilities"]["max_duration"] == 5
    if profile["media_type"] == "video":
        item = roles["duration"]
        assert graph[item["node_id"]]["inputs"][item["field"]] == 124
        assert values["fps"] == 24
        assert values["timeline_duration"] == 5
        loads = [n for n in graph.values() if n["class_type"] == "LoadImage"]
        assert len(loads) == (2 if profile["capability"] == "FIRST_LAST_TO_VIDEO" else 1)
        assert ("end_frame" in roles) == (len(loads) == 2)


def test_codex_pack_covers_all_capabilities_and_matches_image_conditioning():
    capabilities = {
        WorkflowImport.model_validate_json(p.read_text()).capability.value for p in PROFILES
    }
    assert capabilities == {
        "TEXT_TO_IMAGE",
        "IMAGE_TO_IMAGE",
        "IMAGE_TO_VIDEO",
        "FIRST_LAST_TO_VIDEO",
    }
    image = json.loads((PACK / "codex_h3_i2i.api.json").read_text())
    assert any(n["class_type"] == "H3ImageToImagePrepare" for n in image.values())
    assert all(n["class_type"] != "H3ReferenceEditPrepare" for n in image.values())
    selector = next(n for n in image.values() if n["class_type"] == "H3ImageFrameSelector")
    assert selector["inputs"]["strategy"] == "last"
