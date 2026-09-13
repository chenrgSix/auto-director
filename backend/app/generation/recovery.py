"""Replay one interrupted attempt using durable jobs, including its OOM decisions."""

import re
from copy import deepcopy

from app.core.errors import AppError


def prepare_recovery(store, episode, unresolved):
    if not unresolved:
        return episode
    if len(unresolved) != 1 or unresolved[0]["episode_id"] != episode["id"]:
        raise AppError("UNRESOLVED_JOB", "请先恢复其他未结算作业", status=409)
    pending = unresolved[0]
    if not pending.get("comfy_prompt_id"):
        raise AppError(
            "SUBMISSION_UNKNOWN", "请先核对原作业并取得 prompt_id，不能重新提交", status=409
        )
    revision = episode.get("workflow_binding_revision", 0)
    binding = f"binding:{revision}:" if revision else ""
    step = pending["step_key"]
    cursor = None
    if pending["type"] == "SEQUENCE_VIDEO":
        from app.generation.sequence import shot_groups

        members = [s["shot_id"] for s in pending["input_values"]["_sequence"]["segments"]]
        group = next((g for g in shot_groups(episode) if [s["id"] for s in g] == members), None)
        versions = {s["id"]: s["retry_version"] for s in (group or [])}
        if not group or versions != pending["input_values"].get("_sequence_versions"):
            raise AppError("UNRESOLVED_JOB", "原连续组与当前片段版本不匹配，请先核对", status=409)
        jobs = [pending]
    elif pending.get("shot_id"):
        shot = next(
            (s for s in episode["shots"] if s["id"] == pending["shot_id"] and s["enabled"]), None
        )
        prefix = f"{binding}shot:{pending['shot_id']}:r{shot['retry_version']}:" if shot else ""
        match = re.fullmatch(re.escape(prefix) + r"a(\d+):.+", step) if prefix else None
        if not match:
            raise AppError("UNRESOLVED_JOB", "原作业与当前镜头版本不匹配，请先核对", status=409)
        attempt = int(match[1])
        attempt_prefix = f"{prefix}a{attempt}:"
        jobs = [
            j
            for j in store.list("job", episode["id"])
            if j.get("step_key", "").startswith(attempt_prefix)
            and j["created_at"] <= pending["created_at"]
        ]
        cursor = shot.get("render_cursor")
        if (
            not cursor
            or cursor["attempt"] != attempt
            or cursor["retry_version"] != shot["retry_version"]
        ):
            # Legacy jobs predate the cursor. Prior OOM and failed QA consume budget.
            prior_oom = sum(
                j.get("error", {}).get("code") == "OUT_OF_MEMORY"
                for j in store.list("job", episode["id"])
                if j.get("error")
                and j.get("step_key", "").startswith(prefix)
                and not j["step_key"].startswith(attempt_prefix)
            )
            prior_qa = sum(bool(q.get("retry_scope")) for q in shot.get("qa", []))
            original = next(
                (j for j in reversed(jobs) if j["step_key"] == attempt_prefix + "video"), jobs[-1]
            )
            values = original["input_values"]
            budget = original.get("budget_snapshot") or episode["budget"]
            cursor = {
                "attempt": attempt,
                "retry_version": shot["retry_version"],
                "remaining": max(0, budget["max_retries"] - prior_oom - prior_qa),
                "retry_scope": "video"
                if pending["type"] in {"SHOT_VIDEO", "VIDEO_SEGMENT"}
                else "keyframes",
                "base": {
                    **budget,
                    **{
                        k: values[k]
                        for k in (
                            "width",
                            "height",
                            "fps",
                            "batch",
                            "seed",
                            "negative",
                            "motion_strength",
                            "camera_motion",
                            "_oom_recovery",
                        )
                        if k in values
                    },
                },
            }
    else:
        if not step.startswith(binding + "reference:"):
            raise AppError("UNRESOLVED_JOB", "原作业与当前参考图版本不匹配，请先核对", status=409)
        jobs = [pending]
    replay = {}
    for job in jobs:  # Store lists newest first; never replace a later decision with an old one.
        replay.setdefault(job["step_key"], job["id"])
    return store.update(
        "episode",
        episode["id"],
        {
            "render_recovery": {
                "shot_id": pending.get("shot_id"),
                "cursor": deepcopy(cursor),
                "jobs": replay,
                "skip_keyframe_qa": pending["type"]
                in {"SHOT_VIDEO", "VIDEO_SEGMENT", "SHOT_INTERMEDIATE_FRAME", "SEQUENCE_VIDEO"},
            }
        },
    )


def replay_job(store, episode_id, step_key):
    recovery = store.get("episode", episode_id).get("render_recovery") or {}
    id = recovery.get("jobs", {}).get(step_key)
    if not id:
        return None
    job = store.get("job", id)
    if job["episode_id"] != episode_id or job["step_key"] != step_key:
        raise AppError("UNRESOLVED_JOB", "恢复作业身份不匹配", status=409)
    if job["status"] == "FAILED" and (job.get("error") or {}).get("code") == "OUT_OF_MEMORY":
        error = job["error"]
        raise AppError(error["code"], error["message"], error.get("details"))
    return job if job["status"] in {"UNKNOWN", "COMPLETED"} else None


def recovery_profiles(store, episode, profiles):
    recovery = episode.get("render_recovery") or {}
    pinned = {}
    for id in recovery.get("jobs", {}).values():
        job = store.get("job", id)
        if job["episode_id"] != episode["id"]:
            raise AppError("UNRESOLVED_JOB", "恢复工作流身份不匹配", status=409)
        profile = job["profile_snapshot"]
        pinned.setdefault(profile["id"], profile)
    return tuple(deepcopy(pinned.get(profile["id"], profile)) for profile in profiles)
