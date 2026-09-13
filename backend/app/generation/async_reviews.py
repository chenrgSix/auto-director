"""One durable advisory reviewer, independent of the serial media renderer."""

import asyncio
import contextlib
import logging
from copy import deepcopy

from app.agents.directing import Directors
from app.core.errors import AppError
from app.db.store import now, uid
from app.generation.qa_review import current_review_notes, video_review_key

logger = logging.getLogger(__name__)
PENDING = {"pending", "running"}


def visual_qa_enabled(episode, settings):
    return bool(
        episode.get("qa_enabled", True)
        and settings.vlm_model
        and (not episode.get("creation_source") or episode.get("creation_visual_review") == "model")
    )


class AdvisoryReviews:
    def __init__(self, generation):
        self.generation = generation
        self.store = generation.store
        self.queue = asyncio.Queue()
        self.worker = None
        self.current = None
        self.finished = asyncio.Event()
        self.finished.set()
        self.stopping = False

    async def start(self):
        self.stopping = False
        for episode in reversed(self.store.list("episode")):
            for shot in episode.get("shots", []):
                task = shot.get("visual_review") or {}
                if task.get("status") in PENDING:
                    self.queue.put_nowait((episode["id"], shot["id"], task["id"]))
        self.worker = asyncio.create_task(self.consume())

    async def stop(self):
        self.stopping = True
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    def valid(self, episode, shot, task):
        previous = None
        for item in episode["shots"]:
            if item["id"] == shot["id"]:
                break
            if item["enabled"]:
                previous = item
        return (
            visual_qa_enabled(episode, self.generation.settings)
            and episode["status"] != "CANCELLED"
            and shot["enabled"]
            and shot.get("retry_version", 0) == task["retry_version"]
            and video_review_key(episode, shot, previous, self.generation.settings) == task["key"]
        )

    def mutate(self, identity, operation):
        """Fence every result by the current task AND media/configuration, in one transaction."""
        eid, sid, tid = identity

        def transaction(store):
            try:
                episode = store.get("episode", eid)
            except AppError as exc:
                if exc.code == "NOT_FOUND":
                    return None
                raise
            shot = next((s for s in episode["shots"] if s["id"] == sid), None)
            task = (shot or {}).get("visual_review") or {}
            if task.get("id") != tid or task.get("status") not in PENDING:
                return None
            if not self.valid(episode, shot, task):
                task.update(
                    status="skipped", updated_at=now(), reason="复核已关闭、取消或素材已变更"
                )
                store.update("episode", eid, episode)
                return None
            operation(episode, shot, task, store)
            store.update("episode", eid, episode)
            return deepcopy((episode, shot, task))

        return self.store.atomic(transaction)

    def submit(self, episode, shot, previous, samples):
        key = video_review_key(episode, shot, previous, self.generation.settings)
        if (shot.get("qa_video_check") or {}).get("key") == key:
            return
        existing = shot.get("visual_review") or {}
        if existing.get("key") == key and existing.get("status") in PENDING:
            return
        task = {
            "id": uid(),
            "key": key,
            "status": "pending",
            "retry_version": shot.get("retry_version", 0),
            "sample_asset_ids": samples,
            "video_asset_id": shot["video_asset_id"],
            "created_at": now(),
            "updated_at": now(),
        }
        self.generation.update_shot(episode["id"], shot["id"], visual_review=task)
        self.queue.put_nowait((episode["id"], shot["id"], task["id"]))

    async def cancel_episode(self, eid):
        def change(episode):
            for shot in episode["shots"]:
                task = shot.get("visual_review") or {}
                if task.get("status") in PENDING:
                    task.update(status="skipped", updated_at=now(), reason="复核已关闭或取消")

        def cancel_pending(store):
            episode = store.get("episode", eid)
            if any(
                (s.get("visual_review") or {}).get("status") in PENDING for s in episode["shots"]
            ):
                store.update("episode", eid, change)

        self.store.atomic(cancel_pending)
        if self.current and self.current[0] == eid:
            # The consume loop survives cancellation of one review, but stop() exits it.
            finished = self.finished
            self.worker.cancel()
            await finished.wait()

    async def consume(self):
        while True:
            identity = await self.queue.get()
            self.current = identity
            self.finished = asyncio.Event()
            try:
                await self.perform(identity)
            except asyncio.CancelledError:
                if self.stopping:
                    raise
                asyncio.current_task().uncancel()
            except Exception:
                logger.exception("Advisory review worker failed: %s", identity)
            finally:
                self.current = None
                self.finished.set()
                self.queue.task_done()

    async def perform(self, identity):
        snapshot = self.mutate(
            identity, lambda e, s, task, db: task.update(status="running", updated_at=now())
        )
        if not snapshot:
            return
        episode, shot, task = snapshot
        provider = None
        result, error = None, None
        try:
            provider = self.generation.provider_factory()
            images = [self.generation.assets.path(id) for id in task["sample_asset_ids"]]
            async with asyncio.timeout(self.generation.settings.llm_timeout):
                result = await Directors(provider).qa(episode["bible"], shot, images, "video")
        except asyncio.CancelledError:
            self.mutate(identity, lambda e, s, t, db: t.update(status="pending", updated_at=now()))
            raise
        except AppError as exc:
            error = exc
        except TimeoutError:
            error = AppError("LLM_TIMEOUT", "视觉复核超过模型请求超时")
        except Exception:
            logger.exception("Advisory model review failed: %s", identity)
            error = AppError("REVIEW_FAILED", "视觉复核异常，请查看服务日志")

        def publish(episode, shot, task, store):
            notes = current_review_notes(shot)
            replaced = [
                n
                for n in notes
                if n["stage"] == "video" and shot["video_asset_id"] in n.get("asset_ids", [])
            ]
            if replaced:
                shot.setdefault("review_history", []).append(
                    {"replaced_at": now(), "notes": replaced}
                )
                notes = [n for n in notes if n not in replaced]
            shot["qa_video_check"] = {"key": task["key"]}
            if error:
                shot["qa_video_check"]["error_code"] = error.code
                notes.append(
                    {
                        "stage": "video",
                        "code": error.code,
                        "message": f"视觉检查未完成：{error.message}；视频已保留，请人工复核。",
                        "asset_ids": [shot["video_asset_id"]],
                    }
                )
                task.update(status="failed", error=error.as_dict())
            else:
                scope = result.retry_scope(high=episode["quality"] == "high")
                shot.setdefault("qa", []).append(
                    {
                        "stage": "video",
                        **result.model_dump(),
                        "retry_scope": scope,
                        "disposition": "warning" if scope else "passed",
                    }
                )
                store.create(
                    "qa",
                    {
                        "episode_id": episode["id"],
                        "shot_id": shot["id"],
                        "stage": "video",
                        "asset_ids": [shot["video_asset_id"]],
                        "result": result.model_dump(),
                    },
                    parent=episode["id"],
                )
                if scope:
                    notes.append(
                        {
                            "stage": "video",
                            "code": "QA_FAILED",
                            "message": result.explanation,
                            "asset_ids": [shot["video_asset_id"]],
                        }
                    )
                task["status"] = "completed"
            task["updated_at"] = now()
            shot.update(needs_review=bool(notes), review_notes=notes)
            metrics = episode.setdefault("metrics", {})
            for target, source in [
                ("llm_calls", "calls"),
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
            ]:
                metrics[target] = metrics.get(target, 0) + (
                    provider.usage[source] if provider else 0
                )

        self.mutate(identity, publish)
