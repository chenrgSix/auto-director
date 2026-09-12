"""Ordered preview preparation with atomic, source-checked batch checkpoints."""

import asyncio
import hashlib
import json
import time

from app.agents.schemas import ShotPlan
from app.agents.shot_batch import select_prompt_batch
from app.core.errors import AppError
from app.db.store import now, uid
from app.generation.parameters import ai_parameters


def source_key(episode):
    fields = (
        "idea",
        "target_duration",
        "aspect_ratio",
        "style",
        "quality",
        "plan",
        "bible",
        "budget",
        "advanced_mode",
        "workflow_overrides",
        "image_parameters",
        "video_parameters",
        "width",
        "height",
        "fps",
        "seed",
        "max_shot_duration",
        "fixed_shot_duration",
        "image_workflow_id",
        "video_workflow_id",
        "reference_workflow_id",
        "workflow_binding_revision",
    )
    data = {key: episode.get(key) for key in fields}
    data["shots"] = [
        {key: shot.get(key) for key in (*ShotPlan.model_fields, "id", "enabled", "prompts")}
        for shot in episode["shots"]
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def profile_versions(store, profiles):
    return {
        profile["id"]: current.get("configuration_version", current["version"])
        for profile in profiles
        for current in [store.get("workflow", profile["id"])]
    }


def preparation_progress(episode):
    return {
        **(episode.get("prompt_preparation") or {}),
        "completed": sum(bool(shot.get("prompts")) for shot in episode["shots"]),
        "total": len(episode["shots"]),
    }


async def prepare_prompts(service, episode_id, agents, profiles):
    store = service.store
    episode = store.get("episode", episode_id)
    source = source_key(episode)
    # Profiles were selected before model I/O (script review/Bible). Do not bless a newer
    # workflow edit by taking its version here while still using the old parameter schema.
    versions = {p["id"]: p.get("configuration_version", p["version"]) for p in profiles}
    specifications = ai_parameters(*profiles[:2])
    run_id = uid()
    maximum = service.settings.prompt_batch_size

    def guard(current, *, starting=False):
        if current["status"] == "CANCELLED":
            raise AppError("CANCELLED", "单集已取消")
        if (
            current["status"] != "PREPARING_PROMPTS"
            or source_key(current) != source
            or profile_versions(store, profiles) != versions
            or (not starting and (current.get("prompt_preparation") or {}).get("run_id") != run_id)
        ):
            raise AppError(
                "PROMPT_PREPARATION_STALE", "分镜或工作流已变更，请重新准备预览", status=409
            )

    def begin(current):
        guard(current, starting=True)
        current["prompt_preparation"] = {
            "run_id": run_id,
            "status": "running",
            "batch_size": maximum,
            "video_capability": profiles[1].get("capability") if len(profiles) > 1 else None,
            "started_at": now(),
            "active_shot_ids": [],
            "batch_started_at": None,
            "requests": 0,
            "batches": 0,
            "elapsed_seconds": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "recent_batches": [],
        }

    episode = store.update("episode", episode_id, begin)
    index, continuity = 0, {}
    started = None
    provider = agents.provider
    requests_before = 0
    usage_before = {}

    def request_count():
        return getattr(provider, "request_count", provider.usage.get("calls", 0))

    def record(current, status, *, code=None):
        state = current["prompt_preparation"]
        elapsed = round(time.monotonic() - started, 3)
        requests = request_count() - requests_before
        tokens = {
            key: max(0, provider.usage.get(key, 0) - usage_before.get(key, 0))
            for key in ("prompt_tokens", "completion_tokens")
        }
        state["recent_batches"] = [
            *state["recent_batches"][-29:],
            {
                "shot_ids": state["active_shot_ids"],
                "status": status,
                "error_code": code,
                "elapsed_seconds": elapsed,
                "requests": requests,
                **tokens,
            },
        ]
        state["requests"] += requests
        state["elapsed_seconds"] = round(state["elapsed_seconds"] + elapsed, 3)
        state["batches"] += status == "completed"
        for key, value in tokens.items():
            state[key] += value
        state.update(active_shot_ids=[], batch_started_at=None)

    try:
        while index < len(episode["shots"]):
            service.check_cancel(episode_id)
            shot = episode["shots"][index]
            if shot.get("prompts"):
                continuity = shot["prompts"].get("continuity_state", {})
                index += 1
                continue
            batch = select_prompt_batch(
                episode["bible"],
                episode["shots"][index:],
                continuity,
                specifications,
                episode["idea"],
                maximum,
            )

            def waiting(current, batch=batch):
                guard(current)
                current["prompt_preparation"].update(
                    active_shot_ids=[shot["id"] for shot in batch],
                    batch_started_at=now(),
                )

            store.update("episode", episode_id, waiting)
            started, requests_before = time.monotonic(), request_count()
            usage_before = dict(provider.usage)
            if len(batch) == 1:
                prompts = [
                    await agents.shot(
                        episode["bible"],
                        batch[0],
                        continuity,
                        specifications,
                        idea=episode["idea"],
                    )
                ]
            else:
                prompts = await agents.shot_batch(
                    episode["bible"],
                    batch,
                    continuity,
                    specifications,
                    idea=episode["idea"],
                )

            def commit(current, index=index, batch=batch, prompts=prompts):
                guard(current)
                for shot, result in zip(
                    current["shots"][index : index + len(batch)], prompts, strict=True
                ):
                    shot["prompts"] = result.model_dump(mode="json")
                record(current, "completed")

            episode = store.update("episode", episode_id, commit)
            started = None
            source = source_key(episode)
            continuity = episode["shots"][index + len(batch) - 1]["prompts"]["continuity_state"]
            index += len(batch)

        def finish(current):
            guard(current)
            current["prompt_preparation"].update(status="completed", finished_at=now())

        store.update("episode", episode_id, finish)
    except BaseException as exc:
        code = getattr(
            exc,
            "code",
            "WORKER_INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "INTERNAL_ERROR",
        )

        def interrupted(current):
            state = current.get("prompt_preparation") or {}
            if state.get("run_id") != run_id:
                return
            status = "cancelled" if current["status"] == "CANCELLED" else "failed"
            if started is not None:
                record(current, status, code=code)
            state.update(status=status, finished_at=now())

        store.update("episode", episode_id, interrupted)
        raise
