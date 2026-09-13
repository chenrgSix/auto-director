from fastapi import APIRouter, Request, Response

from app.core.errors import AppError
from app.generation.continuity_review import (
    ContinuityReview,
    frame_bytes,
    review_context,
    save_review,
)

router = APIRouter(prefix="/api/v1/episodes", tags=["continuity"])


@router.get("/{episode_id}/shots/{shot_id}/continuity-review")
def read(request: Request, episode_id: str, shot_id: str):
    return review_context(request.app.state.store, episode_id, shot_id)


@router.get("/{episode_id}/shots/{shot_id}/continuity-review/frames/{index}")
async def frame(request: Request, episode_id: str, shot_id: str, index: int, key: str):
    context = review_context(request.app.state.store, episode_id, shot_id)
    if key != context["review_key"]:
        raise AppError("REVIEW_STALE", "素材已变化，请重新读取复核画面", status=409)
    if not 0 <= index < len(context["frames"]):
        raise AppError("NOT_FOUND", "复核帧不存在", status=404)
    content = await frame_bytes(request.app.state.assets, context["frames"][index])
    return Response(
        content, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )


@router.post("/{episode_id}/shots/{shot_id}/continuity-review")
def save(request: Request, episode_id: str, shot_id: str, body: ContinuityReview):
    return save_review(request.app.state.store, episode_id, shot_id, body)
