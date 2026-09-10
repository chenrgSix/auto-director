import asyncio
import contextlib
import json
import logging
import math

from app.agents.directing import Directors, anchored_prompt, generation_budget
from app.agents.provider import LLMProvider
from app.core.config import Settings
from app.core.errors import AppError
from app.db.store import Store, now, uid
from app.generation.engine import RenderEngine
from app.generation.schemas import ACTIVE, EpisodeCreate, TimelineUpdate
from app.media.service import Assets, compose, extract_frame

logger = logging.getLogger(__name__)
CONTINUOUS = {"CONTINUE_FRAME", "CONTINUE_VIDEO"}


class GenerationService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        engine: RenderEngine,
        assets: Assets,
        provider_factory=None,
    ):
        self.settings, self.store, self.engine, self.assets = settings, store, engine, assets
        self.provider_factory = provider_factory or (lambda: LLMProvider(settings))
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.busy: set[str] = set()

    async def start(self) -> None:
        for job in self.store.list("job"):
            if job["status"] == "RUNNING":
                self.store.update(
                    "job",
                    job["id"],
                    {
                        "status": "UNKNOWN",
                        "error": {
                            "code": "WORKER_INTERRUPTED",
                            "message": "服务重启，请读取历史恢复",
                        },
                    },
                )
            elif job["status"] == "QUEUED" and job["type"] == "WORKFLOW_TEST":
                self.queue.put_nowait(("job", job["id"]))
        for episode in reversed(self.store.list("episode")):
            if episode["status"] == "QUEUED":
                self.busy.add(episode["id"])
                self.queue.put_nowait((episode.get("queued_operation", "episode"), episode["id"]))
            elif episode["status"] in ACTIVE:
                self.store.update(
                    "episode",
                    episode["id"],
                    {
                        "status": "FAILED",
                        "error": {
                            "code": "WORKER_INTERRUPTED",
                            "message": "服务重启；已保留计划、镜头和作业，可继续生成",
                        },
                    },
                )
        self.worker = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    async def _consume(self) -> None:
        while True:
            kind, id = await self.queue.get()
            try:
                if kind == "job":
                    await self.engine.run(
                        id, lambda job_id=id: self.store.get("job", job_id)["status"] == "CANCELLED"
                    )
                elif not self.cancelled(id):
                    if kind == "compose":
                        await self.compose_episode(id)
                    else:
                        await self.generate(id)
            except AppError as exc:
                if kind == "job":
                    job = self.store.get("job", id)
                    if job["status"] not in {"UNKNOWN", "CANCELLED", "FAILED"}:
                        self.store.update("job", id, {"status": "FAILED", "error": exc.as_dict()})
                elif not self.cancelled(id):
                    self.store.update("episode", id, {"status": "FAILED", "error": exc.as_dict()})
            except asyncio.CancelledError:
                if kind != "job" and not self.cancelled(id):
                    self.store.update(
                        "episode",
                        id,
                        {
                            "status": "FAILED",
                            "error": {
                                "code": "WORKER_INTERRUPTED",
                                "message": "服务停止；已保存进度",
                            },
                        },
                    )
                raise
            except Exception:
                logger.exception("Generation task failed: %s %s", kind, id)
                self.store.update(
                    "job" if kind == "job" else "episode",
                    id,
                    {
                        "status": "FAILED",
                        "error": {"code": "INTERNAL_ERROR", "message": "任务异常，请查看后端日志"},
                    },
                )
            finally:
                if kind != "job":
                    self.busy.discard(id)
                self.queue.task_done()

    def create(self, request: EpisodeCreate) -> dict:
        defaults = self.store.get("settings", "settings")
        data = request.model_dump()
        for kind in ("image", "video"):
            id = data[f"{kind}_workflow_id"] or defaults[f"default_{kind}"]
            profile = self.store.get("workflow", id)
            if profile["type"] != kind:
                raise AppError("WORKFLOW_INVALID", f"{kind} 工作流类型不匹配")
            data[f"{kind}_workflow_id"] = id
        return self.store.create(
            "episode",
            {
                **data,
                "status": "DRAFT",
                "title": None,
                "plan": None,
                "bible": None,
                "shots": [],
                "references": {},
                "continuity": {},
                "final_video_asset_id": None,
                "error": None,
                "warnings": [],
                "metrics": {},
                "budget": None,
            },
        )

    def enqueue(self, id: str, operation="episode") -> dict:
        if id in self.busy:
            raise AppError("CONFLICT", "上一任务尚未结束，请等待取消完成后重试", status=409)

        def change(episode):
            if episode["status"] in ACTIVE:
                raise AppError("CONFLICT", "此单集已在队列或运行中", status=409)
            if episode["status"] == "COMPLETED" and operation != "compose":
                raise AppError("CONFLICT", "单集已完成；请重试指定镜头或重新导出", status=409)
            episode.update(status="QUEUED", queued_operation=operation, error=None)

        result = self.store.update("episode", id, change)
        self.busy.add(id)
        self.queue.put_nowait((operation, id))
        return result

    def cancelled(self, id: str) -> bool:
        return self.store.get("episode", id)["status"] == "CANCELLED"

    def check_cancel(self, id: str) -> None:
        if self.cancelled(id):
            raise AppError("CANCELLED", "单集已取消")

    def stage(self, id: str, status: str, **values) -> dict:
        def update(episode):
            if episode["status"] == "CANCELLED":
                raise AppError("CANCELLED", "单集已取消")
            episode.update(status=status, **values)

        return self.store.update("episode", id, update)

    def shot(self, episode_id: str, shot_id: str) -> dict:
        return next(s for s in self.store.get("episode", episode_id)["shots"] if s["id"] == shot_id)

    def update_shot(self, episode_id: str, shot_id: str, **values) -> dict:
        self.check_cancel(episode_id)

        def update(episode):
            shot = next(s for s in episode["shots"] if s["id"] == shot_id)
            shot.update(values)

        self.store.update("episode", episode_id, update)
        return self.shot(episode_id, shot_id)

    def warn(self, id: str, message: str) -> None:
        def update(episode):
            if message not in episode["warnings"]:
                episode["warnings"].append(message)

        self.store.update("episode", id, update)

    async def cancel(self, id: str) -> dict:
        episode = self.store.get("episode", id)
        if episode["status"] not in ACTIVE:
            return episode
        result = self.store.update("episode", id, {"status": "CANCELLED"})
        return result

    def find_shot(self, shot_id: str) -> tuple[dict, dict]:
        for episode in self.store.list("episode"):
            for shot in episode["shots"]:
                if shot["id"] == shot_id:
                    return episode, shot
        raise AppError("NOT_FOUND", "镜头不存在", status=404)

    @staticmethod
    def invalidate(shot: dict, scope="keyframes") -> None:
        shot.update(
            video_asset_id=None,
            actual_end_frame_asset_id=None,
            status="STALE",
            error=None,
            qa=[],
            retry_version=shot.get("retry_version", 0) + 1,
        )
        if scope != "video":
            shot.update(start_frame_asset_id=None, end_frame_asset_id=None)

    def retry(self, shot_id: str, scope: str) -> dict:
        episode, _ = self.find_shot(shot_id)
        if episode["status"] in ACTIVE or episode["id"] in self.busy:
            raise AppError("CONFLICT", "请先等待或取消当前生成", status=409)

        def change(current):
            index = next(i for i, shot in enumerate(current["shots"]) if shot["id"] == shot_id)
            self.invalidate(current["shots"][index], scope)
            for shot in current["shots"][index + 1 :]:
                if shot["transition_from_previous"] not in CONTINUOUS:
                    break
                self.invalidate(shot)
            current.update(final_video_asset_id=None, status="FAILED", error=None)

        self.store.update("episode", episode["id"], change)
        return self.enqueue(episode["id"])

    def timeline(self, id: str, request: TimelineUpdate) -> dict:
        if id in self.busy:
            raise AppError("CONFLICT", "当前任务尚未结束，暂不能编辑时间线", status=409)

        def change(episode):
            if episode["status"] in ACTIVE:
                raise AppError("CONFLICT", "运行时不能编辑时间线", status=409)
            by_id = {shot["id"]: shot for shot in episode["shots"]}
            order = [item.id for item in request.shots]
            if len(set(order)) != len(order) or set(order) != set(by_id):
                raise AppError("CONFLICT", "时间线必须包含每个镜头且不能重复", status=409)
            if not any(item.enabled for item in request.shots):
                raise AppError("CONFLICT", "至少保留一个启用镜头", status=409)

            def predecessors(shots):
                previous, result = None, {}
                for shot in shots:
                    if shot["enabled"]:
                        result[shot["id"]] = previous
                        previous = shot["id"]
                return result

            before = predecessors(episode["shots"])
            episode["shots"] = [
                {**by_id[item.id], "enabled": item.enabled, "index": i}
                for i, item in enumerate(request.shots)
            ]
            after = predecessors(episode["shots"])
            invalidated = set()
            for shot in episode["shots"]:
                if (
                    shot["enabled"]
                    and shot["transition_from_previous"] in CONTINUOUS
                    and (
                        before.get(shot["id"]) != after.get(shot["id"])
                        or after.get(shot["id"]) in invalidated
                    )
                ):
                    self.invalidate(shot)
                    invalidated.add(shot["id"])
            episode.update(final_video_asset_id=None, status="DRAFT", error=None)

        return self.store.update("episode", id, change)

    async def render(
        self,
        episode: dict,
        profile: dict,
        type: str,
        values: dict,
        assets: dict,
        key: str,
        shot_id=None,
    ) -> dict:
        self.check_cancel(episode["id"])
        parameters = episode.get(f"{profile['type']}_parameters", {})
        job = self.engine.create_job(
            profile, values, assets, episode["id"], shot_id, type, parameters, key
        )
        results = await self.engine.run(job["id"], lambda: self.cancelled(episode["id"]))
        self.check_cancel(episode["id"])
        return results[0]

    async def generate(self, id: str) -> None:
        episode = self.store.get("episode", id)
        image = self.store.get("workflow", episode["image_workflow_id"])
        video = self.store.get("workflow", episode["video_workflow_id"])
        provider = self.provider_factory()
        agents = Directors(provider)
        try:
            async with self.engine.client() as client:
                system = await client.system()
            budget = episode.get("budget") or generation_budget(
                episode, video["capabilities"], system
            )
            episode = self.stage(
                id, "PLANNING", budget=budget, started_at=episode.get("started_at") or now()
            )
            if not episode["plan"]:
                plan = await agents.plan(episode, budget["max_duration"])
                shots = [
                    {
                        **shot.model_dump(mode="json"),
                        "id": uid(),
                        "enabled": True,
                        "status": "PENDING",
                        "start_frame_asset_id": None,
                        "end_frame_asset_id": None,
                        "video_asset_id": None,
                        "actual_end_frame_asset_id": None,
                        "prompts": None,
                        "qa": [],
                        "error": None,
                        "retry_version": 0,
                    }
                    for shot in plan.shots
                ]
                episode = self.stage(
                    id,
                    "BUILDING_BIBLE",
                    plan=plan.model_dump(mode="json"),
                    title=plan.title,
                    shots=shots,
                )
            if not episode["bible"]:
                self.stage(id, "BUILDING_BIBLE")
                bible = await agents.bible(episode, episode["plan"])
                episode = self.stage(
                    id, "GENERATING_REFERENCES", bible=bible.model_dump(mode="json")
                )
            visual_qa = episode["qa_enabled"] and bool(self.settings.vlm_model)
            if episode["qa_enabled"] and not visual_qa:
                self.warn(id, "未配置 VLM，视觉 QA 已跳过；仅执行媒体技术校验。")
            if "reference_image" not in image["bindings"]:
                self.warn(
                    id,
                    "当前图像工作流无 reference_image 输入，参考资产未用于视觉条件；一致性依赖 Bible 文本。",
                )
            self.stage(id, "GENERATING_REFERENCES")
            descriptions = [
                (
                    f"character:{c['id']}",
                    "CHARACTER_REFERENCE",
                    c["description"] + ". " + ", ".join(c["distinguishing_features"]),
                )
                for c in episode["bible"]["characters"]
            ]
            descriptions += [
                (
                    "environment",
                    "ENVIRONMENT_REFERENCE",
                    json.dumps(episode["bible"]["environment"], ensure_ascii=False),
                ),
                (
                    "style",
                    "STYLE_REFERENCE",
                    json.dumps(episode["bible"]["style"], ensure_ascii=False),
                ),
            ]
            for key, type, prompt in descriptions:
                current = self.store.get("episode", id)
                if key in current["references"]:
                    continue
                result = await self.render(
                    episode,
                    image,
                    type,
                    {
                        **budget,
                        "prompt": anchored_prompt(episode["bible"], prompt, {}),
                        "negative": "text, watermark, artifacts",
                        "seed": episode["seed"] + len(current["references"]),
                    },
                    {},
                    "reference:" + key,
                )
                references = {**current["references"], key: result["id"]}
                self.store.update("episode", id, {"references": references})
            episode = self.store.get("episode", id)
            previous = None
            continuity = {}
            for listed in episode["shots"]:
                if not listed["enabled"]:
                    continue
                shot = self.shot(id, listed["id"])
                if shot["status"] == "PASSED" and shot["video_asset_id"]:
                    previous = shot
                    continuity = shot.get("continuity_after", {})
                    continue
                try:
                    await self.generate_shot(
                        episode, shot, previous, continuity, image, video, budget, agents, visual_qa
                    )
                except AppError as exc:
                    if not self.cancelled(id):
                        self.update_shot(id, shot["id"], status="FAILED", error=exc.as_dict())
                    raise
                previous = self.shot(id, shot["id"])
                continuity = previous.get("continuity_after", {})
                self.store.update("episode", id, {"continuity": continuity})
            await self.compose_episode(id)
        finally:
            jobs = self.store.list("job", id)
            old = self.store.get("episode", id).get("metrics", {})
            self.store.update(
                "episode",
                id,
                {
                    "metrics": {
                        "llm_calls": old.get("llm_calls", 0) + provider.usage["calls"],
                        "prompt_tokens": old.get("prompt_tokens", 0)
                        + provider.usage["prompt_tokens"],
                        "completion_tokens": old.get("completion_tokens", 0)
                        + provider.usage["completion_tokens"],
                        "render_jobs": len(jobs),
                        "image_generations": sum(
                            job["profile_snapshot"]["type"] == "image" for job in jobs
                        ),
                        "video_generations": sum(
                            job["profile_snapshot"]["type"] == "video" for job in jobs
                        ),
                        "failed_jobs": sum(job["status"] == "FAILED" for job in jobs),
                    }
                },
            )

    async def generate_shot(
        self, episode, shot, previous, continuity, image, video, budget, agents, visual_qa
    ):
        id, sid = episode["id"], shot["id"]
        if not shot["prompts"]:
            prompts = await agents.shot(episode["bible"], shot, continuity)
            shot = self.update_shot(id, sid, prompts=prompts.model_dump(mode="json"))
        prompts = shot["prompts"]
        references = episode["references"]
        reference = next(
            (asset for role, asset in references.items() if role.startswith("character:")),
            references.get("environment"),
        )
        ref_inputs = {"reference_image": reference, "style_reference": references["style"]}
        if image["capabilities"].get("supports_multi_reference"):
            ref_inputs.update(
                {f"reference_image_{i + 1}": asset for i, asset in enumerate(references.values())}
            )
        video_inputs = {}
        if previous and shot["transition_from_previous"] == "CONTINUE_VIDEO":
            if video["capabilities"]["supports_video_reference"]:
                video_inputs["reference_video"] = previous["video_asset_id"]
            else:
                self.warn(id, "视频工作流不支持 reference_video，CONTINUE_VIDEO 已降级为帧连续。")
        base = {
            **budget,
            "seed": episode["seed"] + shot["index"] * 100,
            "negative": prompts["negative_prompt"],
            "motion_strength": prompts["motion_strength"],
            "camera_motion": prompts["camera_motion"],
        }
        retry_scope = "keyframes"
        for attempt in range(budget["max_retries"] + 1):
            self.check_cancel(id)
            shot = self.shot(id, sid)
            stamp = f"shot:{sid}:r{shot['retry_version']}:a{attempt}"
            try:
                self.stage(id, "GENERATING_KEYFRAMES")
                if not shot["start_frame_asset_id"]:
                    if previous and shot["transition_from_previous"] in CONTINUOUS:
                        shot = self.update_shot(
                            id,
                            sid,
                            start_frame_asset_id=previous["actual_end_frame_asset_id"],
                            status="START_FRAME_READY",
                        )
                    else:
                        self.update_shot(id, sid, status="GENERATING_START_FRAME")
                        candidate_results = []
                        for candidate in range(budget["candidates"] if visual_qa else 1):
                            result = await self.render(
                                episode,
                                image,
                                "SHOT_START_FRAME",
                                {
                                    **base,
                                    "seed": base["seed"] + attempt * 7 + candidate,
                                    "prompt": anchored_prompt(
                                        episode["bible"], prompts["start_frame_prompt"], continuity
                                    ),
                                },
                                ref_inputs,
                                stamp + f":start:{candidate}",
                                sid,
                            )
                            score = 0
                            if visual_qa and budget["candidates"] > 1:
                                candidate_qa = await agents.qa(
                                    episode["bible"],
                                    shot,
                                    [self.assets.path(result["id"])],
                                    "start_candidate",
                                )
                                score = candidate_qa.score()
                            candidate_results.append((score, result["id"]))
                        best = max(candidate_results, key=lambda item: item[0])[1]
                        shot = self.update_shot(
                            id, sid, start_frame_asset_id=best, status="START_FRAME_READY"
                        )
                if not shot["end_frame_asset_id"]:
                    self.update_shot(id, sid, status="GENERATING_END_FRAME")
                    result = await self.render(
                        episode,
                        image,
                        "SHOT_END_FRAME",
                        {
                            **base,
                            "seed": base["seed"] + 1 + attempt * 7,
                            "prompt": anchored_prompt(
                                episode["bible"],
                                prompts["end_frame_prompt"]
                                + f". Same characters, location and lighting, {shot['duration']} seconds later.",
                                continuity,
                            ),
                        },
                        {**ref_inputs, "reference_image": shot["start_frame_asset_id"]},
                        stamp + ":end",
                        sid,
                    )
                    shot = self.update_shot(
                        id, sid, end_frame_asset_id=result["id"], status="KEYFRAMES_READY"
                    )
                if visual_qa and not shot["video_asset_id"] and retry_scope != "video":
                    qa = await agents.qa(
                        episode["bible"],
                        shot,
                        [
                            self.assets.path(shot["start_frame_asset_id"]),
                            self.assets.path(shot["end_frame_asset_id"]),
                        ],
                        "keyframes",
                    )
                    scope = qa.retry_scope(high=episode["quality"] == "high")
                    self.update_shot(
                        id,
                        sid,
                        qa=shot["qa"]
                        + [{"stage": "keyframes", **qa.model_dump(), "retry_scope": scope}],
                    )
                    if scope:
                        retry_scope = "keyframes" if scope == "video" else scope
                        raise AppError(
                            "QA_FAILED",
                            "关键帧未通过视觉质检",
                            {"scope": retry_scope, "explanation": qa.explanation},
                        )
                if not shot["video_asset_id"]:
                    self.stage(id, "RENDERING_VIDEO")
                    self.update_shot(id, sid, status="RENDERING_VIDEO")
                    result = await self.render_video(
                        episode, shot, image, video, base, ref_inputs, video_inputs, stamp
                    )
                    shot = self.update_shot(
                        id, sid, video_asset_id=result["id"], status="VIDEO_READY"
                    )
                if visual_qa:
                    self.stage(id, "QA")
                    self.update_shot(id, sid, status="QA")
                    samples = []
                    for fraction in (0, 0.5, 1):
                        frame = self.assets.allocate(id, ".png")
                        await extract_frame(
                            self.assets.path(shot["video_asset_id"]), frame, fraction
                        )
                        sample = await self.assets.register(frame, id, "QA_SAMPLE_FRAME", sid)
                        samples.append(self.assets.path(sample["id"]))
                    if previous:
                        samples.append(self.assets.path(previous["actual_end_frame_asset_id"]))
                    qa = await agents.qa(episode["bible"], shot, samples, "video")
                    retry_scope = qa.retry_scope(high=episode["quality"] == "high")
                    self.update_shot(
                        id,
                        sid,
                        qa=self.shot(id, sid)["qa"]
                        + [{"stage": "video", **qa.model_dump(), "retry_scope": retry_scope}],
                    )
                    if retry_scope:
                        raise AppError(
                            "QA_FAILED",
                            "镜头未通过视觉质检",
                            {"scope": retry_scope, "explanation": qa.explanation},
                        )
                last = self.assets.allocate(id, ".png")
                await extract_frame(self.assets.path(shot["video_asset_id"]), last)
                end = await self.assets.register(last, id, "ACTUAL_END_FRAME", sid)
                self.update_shot(
                    id,
                    sid,
                    status="PASSED",
                    actual_end_frame_asset_id=end["id"],
                    continuity_after={**continuity, **prompts["continuity_state"]},
                    error=None,
                )
                return
            except AppError as exc:
                if (
                    exc.code not in {"QA_FAILED", "OUT_OF_MEMORY"}
                    or attempt >= budget["max_retries"]
                ):
                    raise
                if exc.code == "OUT_OF_MEMORY":
                    base.update(
                        width=max(256, (base["width"] * 3 // 4 // 16) * 16),
                        height=max(256, (base["height"] * 3 // 4 // 16) * 16),
                    )
                    self.warn(id, "检测到显存不足，降低生成分辨率后重试，目标时间线保持不变。")
                shot = self.shot(id, sid)
                updates = {"video_asset_id": None, "actual_end_frame_asset_id": None}
                if retry_scope == "keyframes":
                    updates.update(start_frame_asset_id=None, end_frame_asset_id=None)
                elif retry_scope == "transition":
                    updates.update(end_frame_asset_id=None)
                self.update_shot(id, sid, **updates)
        raise AppError("QA_FAILED", "镜头超出重试预算")

    async def render_video(
        self, episode, shot, image, video, base, references, video_inputs, stamp
    ):
        id, sid = episode["id"], shot["id"]
        duration = shot["duration"]
        values = {
            **base,
            "duration": duration,
            "prompt": anchored_prompt(
                episode["bible"],
                shot["prompts"]["video_prompt"],
                shot["prompts"]["continuity_state"],
            ),
        }
        inputs = {
            **video_inputs,
            "start_frame": shot["start_frame_asset_id"],
            "end_frame": shot["end_frame_asset_id"],
            **references,
        }
        try:
            return await self.render(
                episode, video, "SHOT_VIDEO", values, inputs, stamp + ":video", sid
            )
        except AppError as exc:
            if exc.code != "OUT_OF_MEMORY" or episode["budget"]["max_retries"] == 0:
                raise
        smaller = {
            **values,
            "width": max(256, values["width"] // 2 // 16 * 16),
            "height": max(256, values["height"] // 2 // 16 * 16),
            "batch": 1,
        }
        self.warn(id, "视频 OOM：先降低分辨率；若仍失败，使用短分段或已配置的低显存工作流。")
        try:
            return await self.render(
                episode, video, "SHOT_VIDEO", smaller, inputs, stamp + ":video:small", sid
            )
        except AppError as exc:
            if exc.code != "OUT_OF_MEMORY":
                raise
        alternative = video["capabilities"].get("low_memory_workflow_id")
        if alternative:
            video = self.store.get("workflow", alternative)
            if video["type"] != "video":
                raise AppError("WORKFLOW_INVALID", "低显存 profile 必须是视频类型")
        max_segment = min(2, video["capabilities"]["max_duration"])
        count = math.ceil(duration / max_segment)
        if count == 1 and not alternative:
            raise AppError("OUT_OF_MEMORY", "最小批次与降分辨率仍无法渲染，请配置低显存视频工作流")
        if duration / count < 1:
            raise AppError("OUT_OF_MEMORY", "无法在最短 1 秒限制内继续分段")
        segments, start = [], shot["start_frame_asset_id"]
        for index in range(count):
            length = duration / count
            end = shot["end_frame_asset_id"]
            if index < count - 1:
                middle = await self.render(
                    episode,
                    image,
                    "SHOT_INTERMEDIATE_FRAME",
                    {
                        **smaller,
                        "prompt": anchored_prompt(
                            episode["bible"],
                            f"{shot['action']}. Intermediate action state {(index + 1) / count:.2f} of the way from {shot['start_state']} to {shot['end_state']}.",
                            {},
                        ),
                    },
                    {**references, "reference_image": start},
                    stamp + f":middle:{index}",
                    sid,
                )
                end = middle["id"]
            result = await self.render(
                episode,
                video,
                "VIDEO_SEGMENT",
                {**smaller, "duration": length, "seed": base["seed"] + index},
                {**inputs, "start_frame": start, "end_frame": end},
                stamp + f":segment:{index}",
                sid,
            )
            segments.append((self.assets.path(result["id"]), length))
            start = end
        output = self.assets.allocate(id, ".mp4")
        await compose(segments, output, base["width"], base["height"], base["fps"])
        return await self.assets.register(output, id, "SHOT_VIDEO", sid)

    async def compose_episode(self, id: str) -> None:
        episode = self.store.get("episode", id)
        shots = [shot for shot in episode["shots"] if shot["enabled"]]
        if not shots or any(
            shot["status"] != "PASSED" or not shot["video_asset_id"] for shot in shots
        ):
            raise AppError(
                "CONFLICT", "所有启用镜头通过后才能导出，请先生成或重试失效镜头", status=409
            )
        self.stage(id, "COMPOSING")
        output = self.assets.allocate(id, ".mp4")
        budget = episode["budget"]
        metadata = await compose(
            [(self.assets.path(shot["video_asset_id"]), shot["duration"]) for shot in shots],
            output,
            budget["width"],
            budget["height"],
            budget["fps"],
        )
        self.check_cancel(id)
        asset = await self.assets.register(output, id, "FINAL_VIDEO")
        self.stage(
            id,
            "COMPLETED",
            final_video_asset_id=asset["id"],
            completed_at=now(),
            final_duration=metadata["duration"],
            error=None,
        )
