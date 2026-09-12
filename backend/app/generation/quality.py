"""Change future generation policy while retaining the reviewed story and media."""

from copy import deepcopy

from app.agents.directing import quality_budget
from app.core.errors import AppError
from app.db.store import now
from app.generation.schemas import ACTIVE, EpisodeQualityUpdate


def change_quality(store, busy: set[str], id: str, request: EpisodeQualityUpdate) -> dict:
    def check(episode):
        if episode["version"] != request.expected_version:
            raise AppError("CONFLICT", "短片已变更，请关闭编辑后重新打开", status=409)
        jobs = store.list("job", id)
        if (
            id in busy
            or episode["status"] in ACTIVE
            or any(job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in jobs)
        ):
            raise AppError("CONFLICT", "请先等待取消完成并核对未完成作业，再切换质量", status=409)
        return jobs

    current = store.get("episode", id)
    check(current)
    if current["quality"] == request.quality:
        return current

    def change(episode):
        jobs = check(episode)
        episode.setdefault("generation_settings_history", []).append(
            {
                "changed_at": now(),
                "quality": episode["quality"],
                "max_retries": episode.get("max_retries"),
                "budget": deepcopy(episode.get("budget")),
                "render_recovery": deepcopy(episode.get("render_recovery")),
                "shot_execution": {
                    shot["id"]: {
                        key: deepcopy(shot.get(key))
                        for key in (
                            "retry_version",
                            "render_cursor",
                            "qa_retry",
                            "qa_frame_corrections",
                            "status",
                            "error",
                        )
                    }
                    for shot in episode["shots"]
                },
            }
        )
        episode.update(quality=request.quality, max_retries=None)
        if episode.get("budget"):
            # Prepared dimensions/fps/timing stay authoritative for existing footage.
            episode["budget"].update(quality_budget(episode))
        episode.pop("render_recovery", None)
        rendered_shots = {job.get("shot_id") for job in jobs}
        for shot in episode["shots"]:
            if shot["status"] == "PASSED":
                continue
            # A previous quality floor must not force regeneration under the new policy.
            # Retain frames/error so Continue first reinspects them with current thresholds.
            shot.pop("qa_retry", None)
            shot.pop("qa_frame_corrections", None)
            if shot.get("render_cursor") or shot["id"] in rendered_shots:
                # Do not replay old policy snapshots or reuse an exhausted attempt.
                shot["retry_version"] = shot.get("retry_version", 0) + 1
                shot.pop("render_cursor", None)
                shot["status"] = "VIDEO_READY" if shot.get("video_asset_id") else "PENDING"

    return store.update("episode", id, change)
