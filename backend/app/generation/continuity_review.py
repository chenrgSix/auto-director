"""Human or harness review, fenced by the actual adjacent media and reviewed targets."""

import asyncio
import base64
import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.agents.directing import qa_shot_context
from app.agents.provider import vision_data
from app.agents.schemas import StrictModel
from app.core.errors import AppError
from app.db.store import now
from app.media.service import extract_frame


class ContinuityReview(StrictModel):
    request_id: UUID
    expected_version: int = Field(ge=1)
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    verdict: Literal["passed", "needs_changes"]
    notes: str = Field(min_length=1, max_length=3000)


def neighbors(episode, shot_id):
    previous = None
    for shot in episode["shots"]:
        if shot["id"] == shot_id:
            return shot, previous
        if shot["enabled"]:
            previous = shot
    raise AppError("NOT_FOUND", "镜头不属于此短片", status=404)


def review_key(episode, shot, previous):
    def source(item):
        if not item:
            return None
        return {
            "id": item["id"],
            "enabled": item["enabled"],
            "target": qa_shot_context(item),
            "media": [
                item.get(k)
                for k in (
                    "start_frame_asset_id",
                    "end_frame_asset_id",
                    "video_asset_id",
                    "actual_end_frame_asset_id",
                )
            ],
            "references": item.get("reference_selection"),
        }

    payload = {
        "contract": 2,
        "episode": episode["id"],
        "shot": source(shot),
        "previous": source(previous),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def current_review(episode, shot, previous):
    saved = shot.get("continuity_review")
    if not saved:
        return {"status": "pending"}
    return {
        **saved,
        "status": saved["verdict"]
        if saved["review_key"] == review_key(episode, shot, previous)
        else "stale",
    }


def review_context(store, episode_id, shot_id):
    episode = store.get("episode", episode_id)
    shot, previous = neighbors(episode, shot_id)
    frames = []

    def add(label, asset_id, fraction=None):
        if asset_id:
            asset = store.get("asset", asset_id)
            if asset["episode_id"] != episode_id:
                raise AppError("NOT_FOUND", "复核素材不属于此短片", status=404)
            frames.append({"label": label, "asset_id": asset_id, "fraction": fraction})

    if previous:
        add("前镜实际尾部", previous.get("video_asset_id"), 0.95)
        add("前镜实际末帧", previous.get("video_asset_id"), 1)
    add("本镜实际首帧", shot.get("video_asset_id"), 0)
    add("本镜开始动作", shot.get("video_asset_id"), 0.15)
    add("本镜实际末帧", shot.get("video_asset_id"), 1)
    add("本镜首帧目标图", shot.get("start_frame_asset_id"))
    add("本镜尾帧目标图", shot.get("end_frame_asset_id"))
    # Endpoint agreement cannot reveal an unrelated cut inside a generated clip.
    # Append samples to keep existing frame positions stable for callers.
    for label, fraction in (
        ("本镜中前段 35%", 0.35),
        ("本镜中段 50%", 0.5),
        ("本镜中后段 75%", 0.75),
    ):
        add(label, shot.get("video_asset_id"), fraction)
    key = review_key(episode, shot, previous)
    for i, frame in enumerate(frames):
        frame["url"] = (
            f"/api/v1/episodes/{episode_id}/shots/{shot_id}/continuity-review/frames/{i}?key={key}"
        )
    return {
        "episode_id": episode_id,
        "shot_id": shot_id,
        "version": episode["version"],
        "review_key": key,
        "review": current_review(episode, shot, previous),
        "target": qa_shot_context(shot),
        "previous_target": qa_shot_context(previous) if previous else None,
        "reference_selection": shot.get("reference_selection"),
        "frames": frames,
        "limitations": "边界与中段采样可核对站位、道具、动作终点及中途切镜；采样仍可能漏掉短暂异常，完整运动和声音须播放原视频检查。",
    }


async def frame_bytes(assets, spec):
    path = assets.path(spec["asset_id"])
    if spec["fraction"] is None:
        data = await asyncio.to_thread(vision_data, path)
    else:
        with tempfile.TemporaryDirectory(prefix="autodirector-continuity-") as directory:
            frame = Path(directory) / "frame.png"
            await extract_frame(path, frame, spec["fraction"])
            data = await asyncio.to_thread(vision_data, frame)
    return base64.b64decode(data.split(",", 1)[1])


def save_review(store, episode_id, shot_id, request):
    receipt = str(request.request_id)
    payload = request.model_dump(mode="json")
    result = None

    def change(episode):
        nonlocal result
        shot, previous = neighbors(episode, shot_id)
        history = shot.get("continuity_review_history", [])
        prior = next((r for r in history if r["request_id"] == receipt), None)
        if prior:
            if prior["request"] != payload:
                raise AppError("IDEMPOTENCY_CONFLICT", "请求编号已用于另一复核结论", status=409)
            result = prior
            return
        if (
            episode["version"] != request.expected_version
            or review_key(episode, shot, previous) != request.review_key
        ):
            raise AppError("REVIEW_STALE", "镜头、前镜或素材已变化，请重新读取复核画面", status=409)
        if not shot.get("video_asset_id") and not shot.get("start_frame_asset_id"):
            raise AppError("MEDIA_NOT_READY", "尚无画面可供复核", status=409)
        if not request.notes.strip():
            raise AppError("REVIEW_INVALID", "请记录实际观察到的画面依据", status=422)
        result = {
            "request_id": receipt,
            "request": payload,
            "review_key": request.review_key,
            "verdict": request.verdict,
            "notes": request.notes.strip(),
            "reviewed_at": now(),
        }
        shot["continuity_review"] = result
        shot["continuity_review_history"] = [*history, deepcopy(result)]

    # Returning a previous receipt must not advance the episode version.
    def transaction(scoped):
        episode = scoped.get("episode", episode_id)
        before = deepcopy(episode)
        change(episode)
        if episode != before:
            scoped.update("episode", episode_id, episode)
        return result

    return store.atomic(transaction)
