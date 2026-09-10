"""Resolve capability defaults while preserving explicit and legacy selections."""

from app.core.errors import AppError
from app.db.store import Store
from app.workflows.ownership import canonicalize
from app.workflows.schema import WorkflowCapability


class CapabilityRouter:
    def __init__(self, store: Store):
        self.store = store

    def resolve(self, capability: WorkflowCapability | str, workflow_id: str | None = None) -> dict:
        capability = WorkflowCapability(capability)
        if not workflow_id:
            settings = self.store.get("settings", "settings")
            workflow_id = settings.get("default_capabilities", {}).get(capability)
            if not workflow_id:
                legacy_id = settings.get(f"default_{capability.media_type}")
                if legacy_id:
                    legacy = canonicalize(self.store.get("workflow", legacy_id))
                    if legacy["capability"] == capability:
                        workflow_id = legacy_id
        if not workflow_id:
            raise AppError("WORKFLOW_INVALID", f"请配置 {capability} 默认工作流")
        profile = canonicalize(self.store.get("workflow", workflow_id))
        if profile["capability"] != capability:
            raise AppError(
                "WORKFLOW_INVALID",
                "工作流 capability 与请求不匹配",
                {
                    "workflow_id": workflow_id,
                    "required": capability,
                    "actual": profile["capability"],
                },
            )
        return profile

    def select(self, media_type: str, workflow_id: str | None = None) -> dict:
        if media_type not in {"image", "video"}:
            raise AppError("WORKFLOW_INVALID", "未知媒体类型")
        if workflow_id:
            profile = canonicalize(self.store.get("workflow", workflow_id))
            if profile["media_type"] != media_type:
                raise AppError("WORKFLOW_INVALID", f"{media_type} 工作流类型不匹配")
            return profile
        settings = self.store.get("settings", "settings")
        legacy_id = settings.get(f"default_{media_type}")
        if legacy_id:
            # Existing UI selects the preferred capability via the media default.
            selected = self.select(media_type, legacy_id)
            return self.resolve(selected["capability"])
        for capability in WorkflowCapability:
            if capability.media_type == media_type and capability in settings.get(
                "default_capabilities", {}
            ):
                return self.resolve(capability)
        raise AppError("WORKFLOW_INVALID", f"请配置 {media_type} 默认工作流")
