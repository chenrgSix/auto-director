"""Durable image approval gates shared by the webpage and MCP; no session locks."""

import hashlib
import json
from copy import deepcopy
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.agents.schemas import StrictModel
from app.core.errors import AppError
from app.db.store import now
from app.generation.continuity import CONTINUOUS
from app.generation.parameters import parameter_overrides, role_overrides
from app.generation.preview import workflow_versions

WAITING = "AWAITING_IMAGE_REVIEW"


class ImageReviewPause(Exception):
    """Normal worker yield. Persisted episode state owns the pending review."""


class ImageReviewDecision(StrictModel):
    request_id: UUID
    expected_version: int = Field(ge=1)
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "revise"]
    target: str | None = Field(default=None, max_length=100)
    prompt: str = Field(default="", max_length=6000)
    notes: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def require_revision(self):
        if not self.notes.strip():
            raise ValueError("请记录画面观察依据")
        if self.decision == "revise" and (not self.target or not self.prompt.strip()):
            raise ValueError("修改需指定目标图片及完整视觉提示词")
        if self.decision == "approve" and (self.target or self.prompt):
            raise ValueError("确认不能同时修改目标")
        return self


def context(pipeline, episode, stage=None, shot_id=None):
    pending = episode.get("preproduction_pending") or {}
    stage = stage or pending.get("stage")
    shot_id = shot_id or pending.get("shot_id")
    if stage not in {"references", "keyframes"}:
        raise AppError("IMAGE_REVIEW_REQUIRED", "当前没有待确认画面", status=409)
    profiles = pipeline.preview_profiles(episode)
    snapshot = {
        "configuration": {
            k: episode.get(k)
            for k in (
                "budget",
                "quality",
                "seed",
                "width",
                "height",
                "fps",
                "advanced_mode",
                "memory_mode",
            )
        },
        "stage": stage,
        "workflow_versions": workflow_versions(profiles),
        "overrides": episode.get("workflow_overrides", {}),
        "bible": episode["bible"],
        "references": episode["references"],
        "reference_corrections": episode.get("reference_corrections", {}),
    }
    if stage == "references":
        frames = [
            {"target": role, "asset_id": asset, "editable": True}
            for role, asset in episode["references"].items()
        ]
    else:
        shot = next((s for s in episode["shots"] if s["id"] == shot_id and s["enabled"]), None)
        if not shot:
            raise AppError("CONFLICT", "待确认镜头已移除或禁用", status=409)
        enabled = [s for s in episode["shots"] if s["enabled"]]
        index = enabled.index(shot)
        previous = enabled[index - 1] if index else None
        snapshot.update(
            shot_id=shot_id,
            prompts=shot["prompts"],
            transition=shot["transition_from_previous"],
            reference_selection=shot.get("reference_selection"),
            previous_frame=(previous or {}).get("actual_end_frame_asset_id"),
        )
        roles = ["start_frame"] + (
            ["end_frame"] if profiles[1]["capability"] == "FIRST_LAST_TO_VIDEO" else []
        )
        frames = [
            {
                "target": role,
                "asset_id": shot.get(f"{role}_asset_id"),
                "prompt": shot["prompts"].get(f"{role}_prompt", ""),
                "editable": not (
                    role == "start_frame" and shot["transition_from_previous"] in CONTINUOUS
                ),
            }
            for role in roles
        ]
        if previous and previous.get("actual_end_frame_asset_id"):
            frames.insert(
                0,
                {
                    "target": "previous_actual_end",
                    "asset_id": previous["actual_end_frame_asset_id"],
                    "editable": False,
                },
            )
    if not frames or any(not frame["asset_id"] for frame in frames):
        raise AppError("IMAGE_REVIEW_REQUIRED", "目标图片尚未准备完整", status=409)
    for frame in frames:
        asset = pipeline.store.get("asset", frame["asset_id"])
        if asset["episode_id"] != episode["id"] and asset["id"] not in episode.get(
            "allowed_asset_ids", []
        ):
            raise AppError("INVALID_MEDIA", "图片不属于当前制作")
        pipeline.assets.path(asset["id"])
        frame["url"] = f"/api/v1/assets/{asset['id']}/file"
    snapshot["frames"] = frames
    key = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "episode_id": episode["id"],
        "version": episode["version"],
        "stage": stage,
        "shot_id": shot_id if stage == "keyframes" else None,
        "review_key": key,
        "frames": frames,
        "references": episode["references"],
        "limitations": "参考图和关键帧确认只授权下一制作阶段；视频动作、衔接及声音仍需成片复核。",
    }


def gate(pipeline, id, stage, shot_id=None):
    episode = pipeline.store.get("episode", id)
    if not episode.get("image_review_required"):
        return
    current = context(pipeline, episode, stage, shot_id)
    identity = stage + (":" + shot_id if shot_id else "")
    approved = episode.get("image_approvals", {}).get(identity, {})
    if approved.get("review_key") == current["review_key"]:
        return
    pipeline.store.update(
        "episode",
        id,
        {
            "status": WAITING,
            "preproduction_pending": {"stage": stage, "shot_id": shot_id},
            "queued_operation": None,
        },
    )
    raise ImageReviewPause()


def decide(pipeline, id, request):
    payload = request.model_dump(mode="json")
    current = pipeline.store.get("episode", id)
    receipt = current.get("image_review_requests", {}).get(str(request.request_id))
    if receipt:
        if receipt != payload:
            raise AppError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同画面操作", status=409)
        return current

    def change(episode):
        if (
            episode["version"] != request.expected_version
            or id in pipeline.busy
            or episode["status"] != WAITING
        ):
            raise AppError("CONFLICT", "制作状态已变化，请刷新画面后再操作", status=409)
        current = context(pipeline, episode)
        if current["review_key"] != request.review_key:
            raise AppError("CONFLICT", "素材、目标或工作流已变化，请重新查看", status=409)
        identity = current["stage"] + (":" + current["shot_id"] if current["shot_id"] else "")
        record = {
            "at": now(),
            **payload,
            "stage": current["stage"],
            "shot_id": current["shot_id"],
            "frames": deepcopy(current["frames"]),
        }
        if request.decision == "approve":
            episode.setdefault("image_approvals", {})[identity] = record
        else:
            frame = next(
                (f for f in current["frames"] if f["target"] == request.target and f["editable"]),
                None,
            )
            if not frame:
                raise AppError(
                    "CONFLICT",
                    "此图片不可独立修改；连续首帧需修改前镜或在新创作版本中改用切镜",
                    status=409,
                )
            profiles = pipeline.preview_profiles(episode)
            image_profile = profiles[2] if current["stage"] == "references" else profiles[0]
            if "prompt" in role_overrides(
                image_profile, parameter_overrides(episode, image_profile)
            ):
                raise AppError("CONFLICT", "高级模式固定了图像提示词，请先移除该覆盖", status=409)
            if current["stage"] == "keyframes" and request.target in role_overrides(
                profiles[1], parameter_overrides(episode, profiles[1])
            ):
                raise AppError("CONFLICT", "高级模式固定了该帧素材，请先移除该覆盖", status=409)
            if current["stage"] == "references":
                if any(
                    s.get("start_frame_asset_id") or s.get("video_asset_id")
                    for s in episode["shots"]
                ):
                    raise AppError(
                        "CONFLICT",
                        "已经使用参考图制作；请通过新创作版本修改共享参考，避免覆盖既有镜头",
                        status=409,
                    )
                episode["references"].pop(request.target)
                episode.setdefault("reference_corrections", {})[request.target] = (
                    request.prompt.strip()
                )
                revisions = episode.setdefault("reference_revisions", {})
                revisions[request.target] = revisions.get(request.target, 0) + 1
            else:
                shot = next(s for s in episode["shots"] if s["id"] == current["shot_id"])
                if shot.get("video_asset_id"):
                    raise AppError("CONFLICT", "已制作视频的镜头请使用重跑入口", status=409)
                shot["prompts"][request.target + "_prompt"] = request.prompt.strip()
                shot[request.target + "_asset_id"] = None
                if request.target == "start_frame":
                    shot["end_frame_asset_id"] = None
                shot.update(
                    status="PENDING",
                    error=None,
                    retry_version=shot.get("retry_version", 0) + 1,
                    seed_offset=(shot.get("seed_offset", 0) + 10000) % 2147483648,
                )
                for key in ("qa_retry", "render_cursor", "keyframe_comparison"):
                    shot.pop(key, None)
                episode.pop("render_recovery", None)
            episode.setdefault("image_approvals", {}).pop(identity, None)
        episode.setdefault("image_review_history", []).append(record)
        episode.setdefault("image_review_requests", {})[str(request.request_id)] = payload
        episode.update(
            status="QUEUED", queued_operation="episode", preproduction_pending=None, error=None
        )

    result = pipeline.store.update("episode", id, change)
    pipeline.busy.add(id)
    pipeline.queue.put_nowait(("episode", id))
    return result
