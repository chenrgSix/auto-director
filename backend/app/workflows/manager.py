import json
from copy import deepcopy

from app.core.config import ROOT
from app.core.errors import AppError
from app.db.migrations import migrate
from app.db.store import Store, now
from app.workflows.analyzer import analyze, patch, validate_bindings, validate_dependencies
from app.workflows.ownership import canonicalize, decorate_parameters
from app.workflows.schema import WorkflowImport, WorkflowPatch


class WorkflowManager:
    def __init__(self, store: Store):
        self.store = store

    def bootstrap(self) -> None:
        migrate(self.store)
        for id, folder, kind, name in [
            ("default_image", "text_to_image", "image", "SD1.5 · 文生图"),
            ("default_video", "first_last_video", "video", "Wan2.1 FLF · 首尾帧视频"),
        ]:
            try:
                self.store.get("workflow", id)
            except AppError as exc:
                if exc.status != 404:
                    raise
                graph = json.loads((ROOT / "bundled_workflows" / folder / f"{id}.json").read_text())
                self.import_workflow(WorkflowImport(name=name, type=kind, workflow=graph), id=id)
        try:
            self.store.get("settings", "settings")
        except AppError:
            self.store.create(
                "settings",
                {"default_image": "default_image", "default_video": "default_video"},
                id="settings",
            )
        settings = self.store.get("settings", "settings")
        if "default_capabilities" not in settings:
            defaults = {}
            for profile in self.store.list("workflow"):
                defaults.setdefault(profile["capability"], profile["id"])
            for kind in ("image", "video"):
                profile = self.store.get("workflow", settings[f"default_{kind}"])
                defaults[profile["capability"]] = profile["id"]
            self.store.update("settings", "settings", {"default_capabilities": defaults})

    def import_workflow(self, request: WorkflowImport, id=None) -> dict:
        data = request.model_dump()
        analysis = analyze(request.workflow, capability=request.capability)
        if request.bindings is not None:
            analysis["bindings"] = {
                key: value.model_dump() for key, value in request.bindings.items()
            }
        if request.outputs is not None:
            analysis["outputs"] = request.outputs
        record = canonicalize(
            {
                **data,
                **analysis,
                "parameter_values": {},
                "validation": None,
                "last_validated_at": None,
                "last_test_job_id": None,
            }
        )
        return self.describe(
            self.store.create(
                "workflow",
                record,
                id=id,
            )
        )

    def describe(self, profile: dict) -> dict:
        return {
            **profile,
            "binding_assistance": {
                "suggested_capability": None,
                "inputs": {},
                "outputs": {},
            },
            "binding_issues": validate_bindings(profile),
        }

    def update(self, id: str, changes: WorkflowPatch) -> dict:
        original = self.store.get("workflow", id)
        updates = changes.model_dump(exclude_none=True)
        if (
            "capabilities" in updates
            and "capability" not in updates
            and original["media_type"] == "video"
        ):
            updates["capability"] = (
                "FIRST_LAST_TO_VIDEO"
                if updates["capabilities"]["supports_end_frame"]
                else "IMAGE_TO_VIDEO"
            )
        candidate = canonicalize({**deepcopy(original), **updates})
        # Keep incomplete bindings editable; a profile is runnable only after validation.
        if not validate_bindings(candidate):
            patch(candidate, {})
        candidate.update(validation=None, last_validated_at=None)
        return self.describe(self.store.update("workflow", id, candidate))

    def validate(self, id: str, object_info: dict) -> dict:
        profile = self.store.get("workflow", id)
        discovered = analyze(profile["workflow"], object_info)
        profile["parameters"] = discovered["parameters"]
        decorate_parameters(profile)
        result = validate_dependencies(profile, object_info)
        return self.describe(
            self.store.update(
                "workflow",
                id,
                {
                    "parameters": profile["parameters"],
                    "validation": result,
                    "last_validated_at": now(),
                },
            )
        )

    def set_default(self, id: str) -> dict:
        profile = self.store.get("workflow", id)
        issues = validate_bindings(profile)
        if issues:
            raise AppError("WORKFLOW_INVALID", "默认工作流必须具备完整角色绑定", issues)
        settings = self.store.get("settings", "settings")
        return self.store.update(
            "settings",
            "settings",
            {
                f"default_{profile['media_type']}": id,
                "default_capabilities": {
                    **settings.get("default_capabilities", {}),
                    profile["capability"]: id,
                },
            },
        )

    def delete(self, id: str) -> None:
        profile = self.store.get("workflow", id)
        settings = self.store.get("settings", "settings")
        if (
            settings.get(f"default_{profile['media_type']}") == id
            or id in settings.get("default_capabilities", {}).values()
        ):
            raise AppError("CONFLICT", "先设置另一个默认工作流，再删除此项", status=409)
        if any(
            id
            in (
                episode.get("image_workflow_id"),
                episode.get("video_workflow_id"),
                episode.get("reference_workflow_id"),
            )
            for episode in self.store.list("episode")
        ):
            raise AppError("CONFLICT", "工作流已被 Episode 引用", status=409)
        self.store.delete("workflow", id)
