"""Persist a review once, before Bible/prompts/media; retain the original script."""

import hashlib
import json
from copy import deepcopy

from app.agents.schemas import EpisodePlan
from app.agents.script_review import review_script
from app.core.errors import AppError
from app.db.store import now
from app.generation.parameters import parameter_overrides, role_overrides


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def require_review_acknowledgement(episode, accepted):
    review = episode.get("script_review") or {}
    if review.get("status") == "needs_attention":
        if not accepted:
            raise AppError(
                "SCRIPT_REVIEW_REQUIRED",
                "AI 发现尚未解决的剧本问题，请检查后明确确认再生成",
                status=409,
            )
        review["acknowledged_at"] = now()


async def audit_new_script(service, episode, agents, image, video, budget):
    if (episode.get("script_review") or {}).get("status") != "pending":
        return episode
    id = episode["id"]
    source = deepcopy(episode["plan"])
    episode = service.stage(id, "REVIEWING_SCRIPT")
    # Supply only semantic fixed inputs; never send arbitrary workflow/model configuration.
    overrides = {}
    for stage, profile in (("image", image), ("video", video)):
        overrides[stage] = {
            role: value
            for role, value in role_overrides(
                profile, parameter_overrides(episode, profile)
            ).items()
            if role
            in {
                "prompt",
                "negative",
                "camera_motion",
                "start_frame",
                "end_frame",
                "reference_image",
            }
        }
    result = await review_script(agents, episode, image, video, budget, overrides)
    service.check_cancel(id)
    reviewed = deepcopy(source)
    fixes = []
    for fix in result.fixes:
        changes = fix.changes.model_dump(mode="json", exclude_none=True)
        before = {field: source["shots"][fix.shot_index][field] for field in changes}
        reviewed["shots"][fix.shot_index].update(changes)
        fixes.append(
            {"shot_index": fix.shot_index, "reason": fix.reason, "before": before, "after": changes}
        )
    EpisodePlan.model_validate(reviewed)

    def apply(current):
        if (
            plan_hash(current["plan"]) != plan_hash(source)
            or (current.get("script_review") or {}).get("status") != "pending"
            or current["status"] != "REVIEWING_SCRIPT"
        ):
            raise AppError("CONFLICT", "审查期间剧本或任务状态已变化，请重新查看", status=409)
        for shot, updated in zip(current["shots"], reviewed["shots"], strict=True):
            shot.update(updated)
        current["plan"] = reviewed
        current["script_review"] = {
            "status": "needs_attention" if result.issues else "corrected" if fixes else "passed",
            "summary": result.summary,
            "fixes": fixes,
            "issues": [item.model_dump(mode="json") for item in result.issues],
            "original_plan": source,
            "reviewed_plan_hash": plan_hash(reviewed),
            "reviewed_at": now(),
            "edited_after_review": False,
        }
        if result.issues:
            current.update(preview_required=True, preview_approved_at=None)

    return service.store.update("episode", id, apply)
