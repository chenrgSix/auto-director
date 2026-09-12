"""Change semantic QA handling without invalidating reviewed stories or generated media."""

from copy import deepcopy

from app.core.errors import AppError
from app.db.store import now
from app.generation.schemas import ACTIVE, EpisodeQAPolicyUpdate

SEMANTIC_QA_ERRORS = {"QA_FAILED", "KEYFRAMES_TOO_SIMILAR"}


def change_qa_policy(store, busy: set[str], id: str, request: EpisodeQAPolicyUpdate) -> dict:
    def check(episode):
        if episode["version"] != request.expected_version:
            raise AppError("CONFLICT", "短片已变更，请关闭编辑后重新打开", status=409)
        if (
            id in busy
            or episode["status"] in ACTIVE
            or any(
                job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in store.list("job", id)
            )
        ):
            raise AppError("CONFLICT", "请先停止生成并核对未完成作业，再切换质检方式", status=409)

    current = store.get("episode", id)
    check(current)
    if current.get("qa_policy", "strict") == request.qa_policy:
        return current

    def change(episode):
        check(episode)
        episode.setdefault("qa_policy_history", []).append(
            {
                "changed_at": now(),
                "qa_policy": episode.get("qa_policy", "strict"),
                "status": episode["status"],
                "error": deepcopy(episode.get("error")),
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
        episode["qa_policy"] = request.qa_policy
        recovery = episode.get("render_recovery") or {}
        for shot in episode["shots"]:
            if (
                shot["status"] == "PASSED"
                or (shot.get("error") or {}).get("code") not in SEMANTIC_QA_ERRORS
                or recovery.get("shot_id") == shot["id"]
            ):
                continue
            # An old semantic rejection must not discard its usable media on Continue.
            # Technical retries and submitted-job recovery keep their original identity.
            shot.pop("qa_retry", None)
            shot.pop("qa_frame_corrections", None)
            shot.pop("render_cursor", None)
            shot.update(
                retry_version=shot.get("retry_version", 0) + 1,
                status="VIDEO_READY" if shot.get("video_asset_id") else "PENDING",
                error=None,
            )
        if (episode.get("error") or {}).get("code") in SEMANTIC_QA_ERRORS:
            episode["error"] = None
            if episode["status"] == "FAILED":
                episode["status"] = "DRAFT"

    return store.update("episode", id, change)
