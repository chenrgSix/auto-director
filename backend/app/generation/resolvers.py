"""Real-media ownership and continuity resolution, never template placeholder paths."""

from app.core.errors import AppError
from app.generation.continuity import reference_roles, reference_slots
from app.workflows.ownership import is_asset_role, required_asset_roles


class ContinuityManager:
    @staticmethod
    def references(
        episode: dict, shot: dict, profile: dict, start_frame: str | None = None
    ) -> dict:
        references = episode["references"]
        slots = reference_slots(profile)
        if not slots and "style_reference" not in profile["bindings"]:
            return {}
        try:
            roles = reference_roles(episode.get("bible"), shot)
        except AppError as exc:
            if exc.code != "SHOT_REFERENCE_REQUIRED" or not shot.get("start_frame_asset_id"):
                raise
            # Video-only retries of legacy shots already have an unambiguous visual anchor.
            # A fresh start-frame render still requires an explicit character selection.
            roles = []
            start_frame = start_frame or shot["start_frame_asset_id"]
        assets = []
        if start_frame:
            assets.append(start_frame)
        for role in roles:
            if len(assets) >= len(slots):
                break
            if role not in references:
                raise AppError(
                    "SHOT_REFERENCE_MISSING",
                    "本镜参考素材尚未生成",
                    {"shot_id": shot["id"], "role": role},
                    422,
                )
            assets.append(references[role])
        values = dict(zip(slots, assets, strict=False))
        if "style_reference" in profile["bindings"] and "style" in roles:
            values["style_reference"] = references["style"]
        return values

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
        selected = {
            role: asset_id for role, asset_id in bindings.items() if role in profile["bindings"]
        }
        for role, asset_id in selected.items():
            self.validate(role, asset_id, episode_id, allowed, test=test)
        for role, asset_id in selected.items():
            result[role] = await client.upload(self.assets.path(asset_id))
        return result
