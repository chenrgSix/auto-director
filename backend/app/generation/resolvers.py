"""Real-media ownership and continuity resolution, never template placeholder paths."""

from app.core.errors import AppError
from app.workflows.ownership import is_asset_role, required_asset_roles


class ContinuityManager:
    @staticmethod
    def references(episode: dict, start_frame: str | None = None) -> dict:
        references = episode["references"]
        character = next(
            (asset for role, asset in references.items() if role.startswith("character:")),
            references.get("environment"),
        )
        values = {
            "reference_image": start_frame or character,
            "style_reference": references.get("style"),
        }
        values.update(
            {f"reference_image_{i + 1}": asset for i, asset in enumerate(references.values())}
        )
        return {role: asset for role, asset in values.items() if asset}

    @staticmethod
    def video_reference(previous: dict | None, profile: dict) -> dict:
        if previous and profile["capabilities"].get("supports_video_reference"):
            return {"reference_video": previous["video_asset_id"]}
        return {}


class AssetResolver:
    def __init__(self, store, assets):
        self.store, self.assets = store, assets

    def validate(self, role, asset_id, episode_id, allowed=(), *, test=False):
        if not is_asset_role(role):
            raise AppError("WORKFLOW_INVALID", "不允许把资产写入此输入角色", {"role": role})
        asset = self.store.get("asset", asset_id)
        if asset["episode_id"] != episode_id and not test:
            if (
                asset_id not in allowed
                or asset["type"] != "USER_UPLOAD"
                or asset["episode_id"] != "workflow-tests"
            ):
                raise AppError("INVALID_MEDIA", "资产不属于当前 Episode 或显式上传输入")
        expected = (
            "video"
            if role == "reference_video"
            else "audio"
            if role == "reference_audio"
            else "image"
        )
        if asset["metadata"]["kind"] != expected:
            raise AppError("INVALID_MEDIA", f"{role} 需要 {expected} 素材")
        self.assets.path(asset_id)
        return asset

    async def resolve(self, profile, bindings, client, episode_id, allowed=(), *, test=False):
        required = required_asset_roles(profile) | {
            role for role in profile["bindings"] if is_asset_role(role)
        }
        missing = required - {role for role, asset in bindings.items() if asset}
        if missing:
            raise AppError(
                "ASSET_REQUIRED",
                "工作流缺少真实输入素材，不能使用模板占位文件",
                {"roles": sorted(missing)},
            )
        result = {}
        for role, asset_id in bindings.items():
            if role not in profile["bindings"]:
                continue
            self.validate(role, asset_id, episode_id, allowed, test=test)
            result[role] = await client.upload(self.assets.path(asset_id))
        return result
