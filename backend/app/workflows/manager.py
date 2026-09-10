import json

from app.core.config import ROOT
from app.core.errors import AppError
from app.db.store import Store, now
from app.workflows.analyzer import analyze, patch, validate_bindings, validate_dependencies
from app.workflows.schema import WorkflowImport, WorkflowPatch


class WorkflowManager:
    def __init__(self, store: Store):
        self.store = store

    def bootstrap(self) -> None:
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

    def import_workflow(self, request: WorkflowImport, id=None) -> dict:
        data = request.model_dump()
        analysis = analyze(request.workflow)
        if request.bindings is not None:
            analysis["bindings"] = {
                key: value.model_dump() for key, value in request.bindings.items()
            }
        if request.outputs is not None:
            analysis["outputs"] = request.outputs
        return self.store.create(
            "workflow",
            {
                **data,
                **analysis,
                "parameter_values": {},
                "validation": None,
                "last_validated_at": None,
                "last_test_job_id": None,
            },
            id=id,
        )

    def update(self, id: str, changes: WorkflowPatch) -> dict:
        original = self.store.get("workflow", id)
        updates = changes.model_dump(exclude_none=True)
        candidate = {**original, **updates}
        # Keep incomplete bindings editable; a profile is runnable only after validation.
        if not validate_bindings(candidate):
            patch(candidate, {})
        updates.update(validation=None, last_validated_at=None)
        return self.store.update("workflow", id, updates)

    def validate(self, id: str, object_info: dict) -> dict:
        profile = self.store.get("workflow", id)
        discovered = analyze(profile["workflow"], object_info)
        profile["parameters"] = discovered["parameters"]
        for item in profile["parameters"]:
            item["role"] = next(
                (
                    role
                    for role, binding in profile["bindings"].items()
                    if binding["node_id"] == item["node_id"] and binding["input"] == item["field"]
                ),
                None,
            )
        result = validate_dependencies(profile, object_info)
        return self.store.update(
            "workflow",
            id,
            {
                "parameters": profile["parameters"],
                "validation": result,
                "last_validated_at": now(),
            },
        )

    def set_default(self, id: str) -> dict:
        profile = self.store.get("workflow", id)
        issues = validate_bindings(profile)
        if issues:
            raise AppError("WORKFLOW_INVALID", "默认工作流必须具备完整角色绑定", issues)
        return self.store.update("settings", "settings", {f"default_{profile['type']}": id})

    def delete(self, id: str) -> None:
        profile = self.store.get("workflow", id)
        settings = self.store.get("settings", "settings")
        if settings.get(f"default_{profile['type']}") == id:
            raise AppError("CONFLICT", "先设置另一个默认工作流，再删除此项", status=409)
        if any(
            id in (episode.get("image_workflow_id"), episode.get("video_workflow_id"))
            for episode in self.store.list("episode")
        ):
            raise AppError("CONFLICT", "工作流已被 Episode 引用", status=409)
        self.store.delete("workflow", id)
