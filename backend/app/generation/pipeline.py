import asyncio
import contextlib
import logging
import math
from copy import deepcopy

from app.agents.audio import H3, SPEECH
from app.agents.directing import Directors, anchored_prompt, generation_budget
from app.agents.provider import LLMProvider
from app.agents.timing import segment_prompt
from app.core.cancellation import run_cancellable
from app.core.config import Settings
from app.core.errors import AppError
from app.core.limits import MAX_EPISODE_SHOTS
from app.creation.provider import ExternalCreationProvider
from app.db.store import Store, now, uid
from app.generation.async_reviews import AdvisoryReviews, visual_qa_enabled
from app.generation.continuity import continuity_report, reference_description, require_continuity
from app.generation.continuity_review import current_review
from app.generation.engine import RenderEngine
from app.generation.parameters import (
    ai_parameters,
    duration_seconds,
    fit_budget_dimensions,
    parameter_overrides,
    resolve_parameters,
    role_overrides,
    usable_ai_values,
    validate_overrides,
    validate_strategy,
)
from app.generation.preflight import check_episode
from app.generation.preview import (
    apply_edits,
    prepare_review,
    preview_video,
    prompt_view,
    rebind_story,
    refresh_timing_limits,
    require_current_preview,
    validate_review_prompts,
    validate_timing,
    workflow_versions,
)
from app.generation.prompt_optimization import validate_proposal
from app.generation.prompt_preparation import prepare_prompts
from app.generation.qa_retry import (
    consume_retry,
    corrected_prompt,
    correction_reference,
    failed_frames,
    retry_state,
)
from app.generation.qa_review import VIDEO_SAMPLE_FRACTIONS, current_review_notes, video_review_key
from app.generation.recovery import prepare_recovery, recovery_profiles, replay_job
from app.generation.resolvers import AssetResolver, ContinuityManager
from app.generation.schemas import (
    ACTIVE,
    EpisodeCreate,
    EpisodeRerun,
    EpisodeWorkflowRestore,
    EpisodeWorkflowsUpdate,
    PreviewUpdate,
    TimelineUpdate,
)
from app.generation.script_review import audit_new_script, require_review_acknowledgement
from app.generation.workflow_state import (
    REFERENCE_INPUT_WARNING,
    media_ids,
    reconcile_reference_warning,
    recoverable_history,
    resume_story,
    story_snapshot,
)
from app.media.keyframes import inspect_keyframes
from app.media.service import Assets, compose, extract_frame, media_duration
from app.workflows.analyzer import refresh_profile, validate_bindings
from app.workflows.duration import render_maximum
from app.workflows.frame_timing import generation_fps
from app.workflows.ownership import is_asset_role
from app.workflows.router import CapabilityRouter
from app.workflows.schema import WorkflowCapability
from app.workflows.versions import versions_match

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
        self.router = CapabilityRouter(store)
        self.provider_factory = provider_factory or (lambda: LLMProvider(settings))
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.busy: set[str] = set()
        self.reviews = AdvisoryReviews(self)

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
                        **(
                            {
                                "prompt_preparation": {
                                    **episode["prompt_preparation"],
                                    "status": "failed",
                                    "active_shot_ids": [],
                                    "batch_started_at": None,
                                }
                            }
                            if (episode.get("prompt_preparation") or {}).get("status") == "running"
                            else {}
                        ),
                    },
                )
        self.worker = asyncio.create_task(self._consume())
        await self.reviews.start()

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        await self.reviews.stop()

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
                        await self.generate(id, preview_only=kind == "preview")
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
        return self.store.create("episode", self.draft(request))

    def draft(self, request: EpisodeCreate) -> dict:
        """Resolve a new episode without persisting it or invoking a model."""
        data = request.model_dump()
        for kind in ("image", "video"):
            profile = self.router.select(kind, data[f"{kind}_workflow_id"])
            data[f"{kind}_workflow_id"] = profile["id"]
        reference_profile = self.router.resolve(
            WorkflowCapability.TEXT_TO_IMAGE, data["reference_workflow_id"]
        )
        reference_id = reference_profile["id"]
        data["reference_workflow_id"] = reference_id
        selected_ids = {data["image_workflow_id"], data["video_workflow_id"], reference_id}
        if data["workflow_overrides"].keys() - selected_ids:
            raise AppError("WORKFLOW_INVALID", "只能覆盖当前选定工作流的参数")
        allowed = set()
        for workflow_id in selected_ids:
            profile = self.store.get("workflow", workflow_id)
            overrides = parameter_overrides(data, profile)
            validate_overrides(profile, overrides, data["advanced_mode"])
            for role, value in role_overrides(profile, overrides).items():
                if is_asset_role(role):
                    AssetResolver(self.store, self.assets).validate(role, value, "workflow-tests")
                    allowed.add(value)
        data["allowed_asset_ids"] = sorted(allowed)
        return {
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
        }

    def change_workflows(self, id: str, request: EpisodeWorkflowsUpdate) -> dict:
        def change(episode):
            if episode["version"] != request.expected_version:
                raise AppError("CONFLICT", "短片已变更，请关闭编辑后重新打开", status=409)
            if (
                id in self.busy
                or episode["status"] in ACTIVE
                or any(
                    job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"}
                    for job in self.store.list("job", id)
                )
            ):
                raise AppError("CONFLICT", "请先停止生成并核对未完成作业，再更换工作流", status=409)
            selection = request.model_dump(exclude={"expected_version"})
            if all(episode.get(key) == value for key, value in selection.items()):
                return
            rendered = self.has_rendered_content(episode)
            profiles = [
                self.router.select("image", request.image_workflow_id),
                self.router.select("video", request.video_workflow_id),
                self.router.resolve(
                    WorkflowCapability.TEXT_TO_IMAGE, request.reference_workflow_id
                ),
            ]
            for profile in profiles:
                issues = validate_bindings(profile)
                if issues:
                    raise AppError(
                        "WORKFLOW_INVALID", f"{profile['name']} 的输入输出绑定不完整", issues
                    )
            candidate = {**episode, **selection}
            candidate["workflow_overrides"] = {
                key: value
                for key, value in episode.get("workflow_overrides", {}).items()
                if key in selection.values()
            }
            for media in ("image", "video"):
                if episode.get(f"{media}_workflow_id") != selection[f"{media}_workflow_id"]:
                    candidate[f"{media}_parameters"] = {}
            allowed = set(episode.get("allowed_asset_ids", [])) & media_ids(episode)
            for profile in profiles:
                overrides = parameter_overrides(candidate, profile)
                validate_overrides(profile, overrides, candidate.get("advanced_mode", False))
                for role, value in role_overrides(profile, overrides).items():
                    if is_asset_role(role):
                        AssetResolver(self.store, self.assets).validate(
                            role, value, id, episode.get("allowed_asset_ids", [])
                        )
                        allowed.add(value)
            configuration = {
                **selection,
                "workflow_overrides": candidate["workflow_overrides"],
                "image_parameters": candidate.get("image_parameters", {}),
                "video_parameters": candidate.get("video_parameters", {}),
                "allowed_asset_ids": sorted(allowed),
            }
            history = list(episode.get("workflow_binding_history", []))
            history.append(
                {
                    "changed_at": now(),
                    "revision": episode.get("workflow_binding_revision", 0),
                    "previous_state": {
                        **story_snapshot(episode),
                        **{key: deepcopy(episode.get(key)) for key in configuration},
                    },
                }
            )
            episode.update(
                **configuration,
                workflow_binding_revision=episode.get("workflow_binding_revision", 0) + 1,
                workflow_binding_history=history,
            )
            episode.pop("render_recovery", None)
            if rendered and episode.get("plan"):
                resume_story(episode, *profiles)
            elif episode.get("plan"):
                rebind_story(episode, *profiles)
            else:
                episode.update(
                    status="DRAFT",
                    budget=None,
                    error=None,
                    queued_operation=None,
                    preview=None,
                    preview_approved_at=None,
                )
            reconcile_reference_warning(episode, profiles[0])

        return self.store.update("episode", id, change)

    def restore_workflow_story(self, id: str, request: EpisodeWorkflowRestore) -> dict:
        def change(episode):
            if (
                episode["version"] != request.expected_version
                or id in self.busy
                or episode["status"] in ACTIVE
                or self.has_current_jobs(episode)
            ):
                raise AppError("CONFLICT", "短片已变更或仍有作业，请刷新并核对后恢复", status=409)
            history = recoverable_history(episode)
            if not history or history["revision"] != request.history_revision:
                raise AppError(
                    "CONFLICT", "只能恢复空短片中最近一次保留的故事，不能覆盖现有内容", status=409
                )
            source = history["previous_state"]
            # Old snapshots predate preview_required; retain the episode's flag then.
            restored = {**episode, **story_snapshot(source)}
            if restored.get("preview_required") is None:
                restored["preview_required"] = episode.get("preview_required", False)
            restored_ids = media_ids(restored)
            allowed = set(episode.get("allowed_asset_ids", [])) | (
                set(source.get("allowed_asset_ids") or []) & restored_ids
            )
            for asset_id in restored_ids:
                asset = self.store.get("asset", asset_id)
                role = (
                    "reference_video" if asset["metadata"]["kind"] == "video" else "reference_image"
                )
                AssetResolver(self.store, self.assets).validate(role, asset_id, id, allowed)
            restored["allowed_asset_ids"] = sorted(allowed)
            profiles = self.preview_profiles(restored)
            if restored_ids or restored.get("preview_approved_at"):
                resume_story(restored, *profiles)
            else:
                rebind_story(restored, *profiles)
            restored["workflow_recovery"] = {
                "source_revision": history["revision"],
                "binding_revision": episode.get("workflow_binding_revision", 0),
                "recovered_at": now(),
            }
            episode.update(restored)

        return self.store.update("episode", id, change)

    def preview_profiles(self, episode):
        return [
            self.router.select("image", episode.get("image_workflow_id")),
            preview_video(episode, self.router.select("video", episode.get("video_workflow_id"))),
            self.router.resolve(
                WorkflowCapability.TEXT_TO_IMAGE, episode.get("reference_workflow_id")
            ),
        ]

    def detail(self, id: str) -> dict:
        episode = self.store.get("episode", id)
        if REFERENCE_INPUT_WARNING in episode.get("warnings", []):
            try:
                image = self.router.select("image", episode.get("image_workflow_id"))
            except AppError:
                pass  # Keep the original warning if the current workflow cannot be resolved.
            else:
                reconcile_reference_warning(episode, image)
        if history := recoverable_history(episode):
            episode["recoverable_workflow_revision"] = history["revision"]
        if episode["status"] == "AWAITING_REVIEW" and episode.get("preview"):
            profiles = self.preview_profiles(episode)
            if versions_match(episode["preview"]["workflow_versions"], profiles):
                # Refresh derived views for old previews without rewriting user data/version.
                refresh_timing_limits(episode, profiles[1])
                for shot in episode["shots"]:
                    shot["preview_prompt_view"] = prompt_view(episode, shot, *profiles[:2])
        if episode["status"] == "AWAITING_REVIEW" and episode.get("bible") and episode.get("shots"):
            try:
                episode["continuity_report"] = continuity_report(
                    episode, self.preview_profiles(episode)[0]
                )
            except AppError:
                pass  # Historical media stays readable when its workflow has been removed.
        previous = None
        for shot in episode["shots"]:
            if shot.get("continuity_review"):
                shot["continuity_review_status"] = current_review(episode, shot, previous)
                verdict = shot["continuity_review_status"]["status"]
                if verdict == "passed":
                    shot["needs_review"] = False
                elif verdict in {"needs_changes", "stale"}:
                    shot["needs_review"] = True
            if shot.get("enabled", True):
                previous = shot
        return episode

    def has_current_jobs(self, episode):
        revision = episode.get("workflow_binding_revision", 0)
        for job in self.store.list("job", episode["id"]):
            if job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"}:
                return True
            step = job.get("step_key", "")
            if (
                step.startswith(f"binding:{revision}:")
                if revision
                else not step.startswith("binding:")
            ):
                return True
        return False

    def has_rendered_content(self, episode):
        return bool(
            self.has_current_jobs(episode)
            or episode.get("references")
            or episode.get("final_video_asset_id")
            or any(
                shot.get(field)
                for shot in episode.get("shots", [])
                for field in (
                    "start_frame_asset_id",
                    "end_frame_asset_id",
                    "actual_end_frame_asset_id",
                    "video_asset_id",
                )
            )
        )

    def check_review_state(self, episode, expected_version):
        if episode["version"] != expected_version:
            raise AppError("CONFLICT", "分镜已变更，请重新载入后再确认", status=409)
        if (
            episode["id"] in self.busy
            or episode["status"] != "AWAITING_REVIEW"
            or episode.get("preview_approved_at")
        ):
            raise AppError("CONFLICT", "当前状态不能编辑或确认分镜", status=409)

    def edit_preview(self, id: str, request: PreviewUpdate) -> dict:
        def change(episode):
            self.check_review_state(episode, request.expected_version)
            profiles = self.preview_profiles(episode)
            require_current_preview(episode, profiles)
            before = (
                deepcopy(episode["plan"]),
                [deepcopy(s.get("prompts")) for s in episode["shots"]],
            )
            apply_edits(episode, request, *profiles[:2])
            if episode.get("script_review") and before != (
                episode["plan"],
                [s.get("prompts") for s in episode["shots"]],
            ):
                episode["script_review"]["edited_after_review"] = True
                episode["script_review"].pop("acknowledged_at", None)

        return self.store.update("episode", id, change)

    def enqueue(
        self,
        id: str,
        operation="episode",
        expected_version=None,
        *,
        accept_script_review=False,
        request_id=None,
    ) -> dict:
        request = {
            "operation": operation,
            "expected_version": expected_version,
            "accept_script_review": accept_script_review,
        }
        if request_id:
            current = self.store.get("episode", id)
            previous = current.get("operation_requests", {}).get(request_id)
            if previous:
                if previous != request:
                    raise AppError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同制作操作", status=409)
                return current
        if id in self.busy:
            raise AppError("CONFLICT", "上一任务尚未结束，请等待取消完成后重试", status=409)

        def change(episode):
            if episode.get("creation_source") and operation == "preview":
                raise AppError(
                    "CREATION_PACKAGE_REQUIRED", "请在创作项目中校验并提交新版本", status=409
                )
            if operation == "approve":
                self.check_review_state(episode, expected_version)
                profiles = self.preview_profiles(episode)
                require_current_preview(episode, profiles)
                validate_timing(episode, profiles[1])
                validate_review_prompts(episode, *profiles[:2])
                require_continuity(episode, profiles[0])
                require_review_acknowledgement(episode, accept_script_review)
                if episode.get("creation_source"):
                    self.validate_external_prompts(episode, profiles)
                episode["preview_approved_at"] = now()
            elif operation == "preview":
                if (
                    episode["status"] in ACTIVE
                    or episode.get("preview_approved_at")
                    or self.has_rendered_content(episode)
                ):
                    raise AppError("CONFLICT", "已开始渲染或任务尚未结束，不能重新预览", status=409)
                profiles = self.preview_profiles(episode)
                previous = episode.get("preview") or {}
                if not versions_match(previous.get("workflow_versions", {}), profiles):
                    if episode.get("plan"):
                        rebind_story(episode, *profiles)
                    else:
                        episode.update(budget=None)
                episode.update(
                    preview_required=True,
                    preview={"workflow_versions": workflow_versions(profiles)},
                )
            elif episode.get("preview_required") and not episode.get("preview_approved_at"):
                raise AppError("PREVIEW_REQUIRED", "请先查看分镜并确认开始视频生成", status=409)
            if episode["status"] in ACTIVE:
                raise AppError("CONFLICT", "此单集已在队列或运行中", status=409)
            if episode["status"] == "COMPLETED" and operation != "compose":
                raise AppError("CONFLICT", "单集已完成；请重试指定镜头或重新导出", status=409)
            episode.update(status="QUEUED", queued_operation=operation, error=None)
            if request_id:
                episode.setdefault("operation_requests", {})[request_id] = request

        result = self.store.update("episode", id, change)
        self.busy.add(id)
        self.queue.put_nowait((operation, id))
        return result

    def validate_external_prompts(self, episode, profiles):
        from pydantic import ValidationError

        from app.agents.audio import audio_output
        from app.agents.parameters import constrained_output
        from app.agents.schemas import ShotPrompts

        self.require_creation_review_model(episode)
        schema = audio_output(
            constrained_output(ShotPrompts, ai_parameters(*profiles[:2])),
            ai_parameters(*profiles[:2]),
        )
        try:
            for shot in episode["shots"]:
                schema.model_validate(shot.get("prompts"))
        except ValidationError as exc:
            raise AppError(
                "CREATION_PACKAGE_REQUIRED",
                "提示词不完整或不符合当前制作约束，请修改创作包",
                status=422,
            ) from exc

    def require_creation_review_model(self, episode):
        if (
            episode.get("creation_source")
            and episode.get("creation_visual_review") == "model"
            and not self.settings.vlm_model
        ):
            raise AppError(
                "CONFIGURATION_REQUIRED",
                "此制作选择了视觉模型复核，请先恢复视觉模型配置",
                status=409,
            )

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

    def record_qa(
        self, episode_id: str, shot_id: str, stage: str, result, assets: list[str]
    ) -> None:
        self.store.create(
            "qa",
            {
                "episode_id": episode_id,
                "shot_id": shot_id,
                "stage": stage,
                "asset_ids": assets,
                "result": result.model_dump(),
            },
            parent=episode_id,
        )

    def note_review(self, id, sid, stage, code, message, asset_ids):
        shot = self.shot(id, sid)
        notes = current_review_notes(shot)
        note = {"stage": stage, "code": code, "message": message, "asset_ids": asset_ids}
        if note not in notes:
            notes.append(note)
        self.update_shot(id, sid, needs_review=True, review_notes=notes)

    async def review_video(self, episode, shot, previous, agents, *, advisory):
        id, sid = episode["id"], shot["id"]
        key = video_review_key(episode, shot, previous, self.settings)
        cached = shot.get("qa_video_check") or {}
        if advisory and cached.get("key") == key:
            return

        def replace_previous_review():
            current = self.shot(id, sid)
            notes = current_review_notes(current)
            previous_notes = [
                note
                for note in notes
                if note["stage"] == "video" and shot["video_asset_id"] in note.get("asset_ids", [])
            ]
            if previous_notes:
                notes = [note for note in notes if note not in previous_notes]
                self.update_shot(
                    id,
                    sid,
                    review_notes=notes,
                    needs_review=bool(notes),
                    review_history=current.get("review_history", [])
                    + [{"replaced_at": now(), "notes": previous_notes}],
                )

        if not advisory:
            self.stage(id, "QA")
            self.update_shot(id, sid, status="QA")
        samples = []
        # Decoding errors remain hard failures. Only model review errors are advisory.
        for fraction in VIDEO_SAMPLE_FRACTIONS:
            frame = self.assets.allocate(id, ".png")
            await extract_frame(
                self.assets.path(shot["video_asset_id"]),
                frame,
                fraction,
            )
            sample = await self.assets.register(frame, id, "QA_SAMPLE_FRAME", sid)
            samples.append(sample["id"])
        if previous:
            samples.append(previous["actual_end_frame_asset_id"])
        if advisory:
            current = self.store.get("episode", id)
            if visual_qa_enabled(current, self.settings):
                self.reviews.submit(current, self.shot(id, sid), previous, samples)
            return
        qa = await agents.qa(
            episode["bible"], shot, [self.assets.path(asset) for asset in samples], "video"
        )
        scope = qa.retry_scope(high=episode["quality"] == "high")
        self.record_qa(id, sid, "video", qa, [shot["video_asset_id"]])
        entry = {"stage": "video", **qa.model_dump(), "retry_scope": scope}
        self.update_shot(id, sid, qa=self.shot(id, sid)["qa"] + [entry])
        replace_previous_review()
        if scope:
            raise AppError(
                "QA_FAILED",
                "镜头未通过视觉质检",
                {"scope": scope, "explanation": qa.explanation},
            )

    async def cancel(self, id: str) -> dict:
        episode = self.store.get("episode", id)
        if episode["status"] not in ACTIVE:
            await self.reviews.cancel_episode(id)
            return episode
        result = self.store.update("episode", id, {"status": "CANCELLED"})
        # Queue operations are synchronous here; the worker cannot take an item mid-removal.
        for _ in range(self.queue.qsize()):
            kind, queued_id = self.queue.get_nowait()
            if kind != "job" and queued_id == id:
                self.busy.discard(id)
            else:
                self.queue.put_nowait((kind, queued_id))
            self.queue.task_done()
        await self.reviews.cancel_episode(id)
        return result

    def find_shot(self, shot_id: str) -> tuple[dict, dict]:
        for episode in self.store.list("episode"):
            for shot in episode["shots"]:
                if shot["id"] == shot_id:
                    return episode, shot
        raise AppError("NOT_FOUND", "镜头不存在", status=404)

    @staticmethod
    def invalidate(shot: dict, scope="keyframes", *, frames=None) -> None:
        shot.pop("actual_duration", None)
        shot.pop("full_tail_video_asset_id", None)
        shot.pop("render_cursor", None)
        shot.pop("keyframe_comparison", None)
        shot.pop("qa_retry", None)
        shot.pop("qa_frame_corrections", None)
        shot.pop("qa_video_check", None)
        shot.pop("visual_review", None)
        shot.pop("optimization_attempt", None)
        shot.pop("optimization_reference_assets", None)
        shot.update(
            video_asset_id=None,
            actual_end_frame_asset_id=None,
            status="STALE",
            error=None,
            qa=[],
            needs_review=False,
            review_notes=[],
            retry_version=shot.get("retry_version", 0) + 1,
        )
        if frames is not None:
            for role in frames:
                shot[f"{role}_asset_id"] = None
        elif scope != "video":
            shot.update(start_frame_asset_id=None, end_frame_asset_id=None)
        if scope == "prompts":
            previous = shot.get("prompts") or {}
            if "allow_static_end_frame" in previous:
                shot["pending_static_end_frame"] = previous["allow_static_end_frame"]
            shot["prompts"] = None
            for field in ("preview_prompt_view", "legacy_ai_prompt_parameters", "continuity_after"):
                shot.pop(field, None)
            shot["preview_edited_fields"] = [
                field
                for field in shot.get("preview_edited_fields", [])
                if field not in {"start_frame_prompt", "end_frame_prompt", "video_prompt"}
            ]

    def retry(self, shot_id: str, scope: str) -> dict:
        episode, _ = self.find_shot(shot_id)
        return self.rerun(
            episode["id"],
            EpisodeRerun(expected_version=episode["version"], scope=scope, shot_ids=[shot_id]),
        )

    def rerun(self, id: str, request: EpisodeRerun, *, optimization=None, request_id=None) -> dict:
        payload = request.model_dump(mode="json")
        if request_id:
            current = self.store.get("episode", id)
            receipt = current.get("rerun_requests", {}).get(request_id)
            if receipt:
                if receipt != payload:
                    raise AppError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同重跑内容", status=409)
                return current
        if id in self.busy:
            raise AppError("CONFLICT", "请先等待或取消当前生成", status=409)
        if self.engine.unresolved():
            raise AppError(
                "UNRESOLVED_JOB", "存在未确认的作业，请先恢复或核对原任务再重跑", status=409
            )
        if any(j["status"] in {"RUNNING", "QUEUED"} for j in self.store.list("job", id)):
            raise AppError("CONFLICT", "当前仍有未结束的作业，暂不能重跑", status=409)

        def change(current):
            if current.get("creation_source") and request.scope == "prompts":
                raise AppError(
                    "CREATION_PACKAGE_REQUIRED", "请在创作项目中修改提示词并提交新版本", status=409
                )
            if request_id:
                current.setdefault("rerun_requests", {})[request_id] = payload
            if current["version"] != request.expected_version:
                raise AppError("CONFLICT", "短片已更新，请刷新后重新选择重跑范围", status=409)
            if current["status"] in ACTIVE:
                raise AppError("CONFLICT", "请先等待或取消当前生成", status=409)
            if optimization is not None:
                validate_proposal(self, current, optimization)
            if current.get("preview_required") and not current.get("preview_approved_at"):
                raise AppError("PREVIEW_REQUIRED", "请先确认分镜", status=409)
            enabled = {s["id"] for s in current["shots"] if s["enabled"]}
            selected = set(request.shot_ids) if request.shot_ids is not None else enabled
            if (
                not selected
                or not selected <= enabled
                or (request.shot_ids is not None and len(selected) != len(request.shot_ids))
            ):
                raise AppError("CONFLICT", "请选择当前短片中启用且不重复的镜头", status=409)
            scopes, previous_changed = {}, False
            for shot in current["shots"]:
                if not shot["enabled"]:
                    continue
                dependent = previous_changed and shot["transition_from_previous"] in CONTINUOUS
                if shot["id"] in selected:
                    scopes[shot["id"]] = (
                        "keyframes" if dependent and request.scope == "video" else request.scope
                    )
                elif dependent:
                    scopes[shot["id"]] = "keyframes"
                previous_changed = shot["id"] in scopes
            snapshot = {
                "id": uid(),
                "created_at": now(),
                "scope": request.scope,
                "shot_ids": sorted(selected),
                "affected_shot_ids": list(scopes),
                "new_seed": request.new_seed,
                "final_video_asset_id": current.get("final_video_asset_id"),
                "final_duration": current.get("final_duration"),
                "shots": [deepcopy(s) for s in current["shots"] if s["id"] in scopes],
            }
            current.setdefault("rerun_history", []).append(snapshot)
            if optimization is not None:
                snapshot["prompt_optimization"] = deepcopy(optimization)
            current.pop("render_recovery", None)
            for shot in current["shots"]:
                if shot["id"] in scopes:
                    optimizing = optimization is not None and shot["id"] == optimization["shot_id"]
                    old_frames = {
                        role: shot.get(f"{role}_asset_id")
                        for role in (optimization["frames"] if optimizing else [])
                    }
                    self.invalidate(
                        shot,
                        scopes[shot["id"]],
                        frames=optimization["frames"] if optimizing else None,
                    )
                    if optimization is not None:
                        shot["optimization_attempt"] = {
                            "id": optimization["id"],
                            "retry_version": shot["retry_version"],
                        }
                    if optimizing:
                        for field, change in optimization["changes"].items():
                            shot["prompts"][field] = change["prompt"]
                        shot["optimization_reference_assets"] = old_frames
                        shot.pop("preview_prompt_view", None)
                    if shot["id"] in selected and request.allow_static_end_frame is not None:
                        if shot.get("prompts"):
                            shot["prompts"]["allow_static_end_frame"] = (
                                request.allow_static_end_frame
                            )
                        else:
                            shot["pending_static_end_frame"] = request.allow_static_end_frame
                    if request.new_seed:
                        shot["seed_offset"] = (shot.get("seed_offset", 0) + 10000) % 2147483648
            current.update(
                final_video_asset_id=None,
                final_duration=None,
                status="QUEUED",
                queued_operation="episode",
                error=None,
            )

        result = self.store.update("episode", id, change)
        self.busy.add(id)
        self.queue.put_nowait(("episode", id))
        return result

    def timeline(self, id: str, request: TimelineUpdate) -> dict:
        if id in self.busy:
            raise AppError("CONFLICT", "当前任务尚未结束，暂不能编辑时间线", status=409)
        if any(
            job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in self.store.list("job", id)
        ):
            raise AppError("CONFLICT", "请先恢复或核对未结束作业，再编辑时间线", status=409)

        def change(episode):
            if episode["status"] in ACTIVE:
                raise AppError("CONFLICT", "运行时不能编辑时间线", status=409)
            if episode.get("preview_required") and not episode.get("preview_approved_at"):
                raise AppError("PREVIEW_REQUIRED", "请使用分镜预览编辑时长与提示词", status=409)
            if (
                len(request.shots) > MAX_EPISODE_SHOTS
                and len(episode["shots"]) <= MAX_EPISODE_SHOTS
            ):
                raise AppError("LIMIT_EXCEEDED", "新时间线最多 240 镜")
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
            episode.pop("render_recovery", None)

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
        step_key = (
            f"binding:{episode['workflow_binding_revision']}:{key}"
            if episode.get("workflow_binding_revision")
            else key
        )
        if existing := replay_job(self.store, episode["id"], step_key):
            results = await self.engine.run(existing["id"], lambda: self.cancelled(episode["id"]))
            self.check_cancel(episode["id"])
            return results[0]
        parameters = parameter_overrides(episode, profile)
        output = (
            self.shot(episode["id"], shot_id).get("prompts") if shot_id else episode.get("bible")
        ) or {}
        generated = usable_ai_values(
            profile,
            output.get("ai_parameters", {}).get(profile["id"], {}),
            stage_prompt="prompt" in values,
        )
        job = self.engine.create_job(
            profile,
            values,
            assets,
            episode["id"],
            shot_id,
            type,
            parameters,
            step_key,
            advanced_mode=episode.get("advanced_mode", False),
            budget=episode.get("budget"),
            allowed_asset_ids=episode.get("allowed_asset_ids", []),
            ai_values=generated,
        )
        results = await self.engine.run(job["id"], lambda: self.cancelled(episode["id"]))
        self.check_cancel(episode["id"])
        return results[0]

    async def generate(self, id: str, *, preview_only=False) -> None:
        episode = self.store.get("episode", id)
        if not preview_only:
            episode = prepare_recovery(self.store, episode, self.engine.unresolved())
        image = self.router.select("image", episode.get("image_workflow_id"))
        video = self.router.select("video", episode.get("video_workflow_id"))
        reference_profile = self.router.resolve(
            WorkflowCapability.TEXT_TO_IMAGE, episode.get("reference_workflow_id")
        )
        image, video, reference_profile = recovery_profiles(
            self.store, episode, (image, video, reference_profile)
        )
        provider = (
            ExternalCreationProvider(
                self.provider_factory, visual_review=episode.get("creation_visual_review")
            )
            if episode.get("creation_source")
            else self.provider_factory()
        )

        def check_cancel():
            self.check_cancel(id)

        agents = Directors(provider, check_cancel=check_cancel)
        preview_complete = False
        try:
            self.require_creation_review_model(episode)
            if episode.get("preview_required") and not preview_only:
                if not episode.get("preview_approved_at"):
                    raise AppError("PREVIEW_REQUIRED", "请先确认分镜", status=409)
                if not self.has_rendered_content(episode) and not episode.get(
                    "refresh_workflow_budget"
                ):
                    try:
                        require_current_preview(episode, (image, video, reference_profile))
                    except AppError:
                        self.store.update("episode", id, {"preview_approved_at": None})
                        raise
            async with self.engine.client() as client:
                system = await run_cancellable(client.system, check_cancel)
                object_info = await run_cancellable(client.object_info, check_cancel)
            image, video, reference_profile = (
                refresh_profile(profile, object_info, parameter_overrides(episode, profile))
                for profile in (image, video, reference_profile)
            )
            fresh_budget = generation_budget(
                episode, video["capabilities"], system, remote_video=video["remote_video"]
            )
            budget = dict(
                fresh_budget
                if episode.get("refresh_workflow_budget")
                else episode.get("budget") or fresh_budget
            )
            # Discard legacy VRAM/quality duration caps for local and remote workflows.
            # Keep prepared content, dimensions and explicit user duration limits intact.
            for key in ("max_duration", "render_max_duration"):
                budget[key] = fresh_budget[key]
            override_roles = role_overrides(video, parameter_overrides(episode, video))
            budget["fps"] = generation_fps(video, budget["fps"], override_roles.get("fps"))
            budget["render_max_duration"] = render_maximum(video, budget)
            budget["max_duration"] = min(budget["max_duration"], budget["render_max_duration"])
            validate_overrides(
                video, parameter_overrides(episode, video), episode.get("advanced_mode", False)
            )
            ceilings = dict(budget)
            budget.update(
                {
                    role: value
                    for role, value in override_roles.items()
                    if role in {"width", "height", "fps", "batch", "seed"}
                }
            )
            validate_strategy(
                budget, budget["max_duration"], low_memory=budget["low_memory"], ceilings=ceilings
            )
            fit_budget_dimensions(episode, video, budget)
            if not preview_only:
                check_episode(episode, image, video, reference_profile, budget, object_info)
            fixed_duration = None
            if "duration" in override_roles:
                fixed_duration = duration_seconds(video, override_roles["duration"], budget["fps"])
                validate_strategy({"duration": fixed_duration}, budget["max_duration"])
            episode = self.stage(
                id,
                "PLANNING",
                budget=budget,
                refresh_workflow_budget=False,
                fixed_shot_duration=fixed_duration,
                started_at=episode.get("started_at") or now(),
            )
            if not episode["plan"]:
                plan = await agents.plan(
                    episode, budget["max_duration"], check_cancel=lambda: self.check_cancel(id)
                )
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
                    "REVIEWING_SCRIPT",
                    plan=plan.model_dump(mode="json"),
                    title=plan.title,
                    shots=shots,
                    script_review={"status": "pending"},
                )
            if not episode.get("creation_source"):
                episode = await audit_new_script(self, episode, agents, image, video, budget)
            if (episode.get("script_review") or {}).get(
                "status"
            ) == "needs_attention" and not episode.get("preview_approved_at"):
                # Legacy direct-generation clients must also inspect unresolved script problems.
                preview_only = True
            # Check every remaining clip before spending on references or keyframes.
            for shot in episode["shots"]:
                if shot["enabled"] and not shot["video_asset_id"]:
                    resolve_parameters(
                        video,
                        {**budget, "duration": shot["duration"]},
                        {},
                        parameter_overrides(episode, video),
                        episode.get("advanced_mode", False),
                        budget,
                    )
            if not episode["bible"]:
                self.stage(id, "BUILDING_BIBLE")
                bible = await agents.bible(
                    episode, episode["plan"], ai_parameters(reference_profile)
                )
                episode = self.stage(id, "BUILDING_BIBLE", bible=bible.model_dump(mode="json"))
            if preview_only or any(s["enabled"] and not s.get("prompts") for s in episode["shots"]):
                self.stage(id, "PREPARING_PROMPTS")
                await prepare_prompts(self, id, agents, (image, video, reference_profile))
                self.check_cancel(id)
                episode = self.store.get("episode", id)
            if preview_only:
                self.store.update(
                    "episode",
                    id,
                    lambda current: prepare_review(current, image, video, reference_profile),
                )
                preview_complete = True
                return
            require_continuity(episode, image)
            visual_qa = visual_qa_enabled(episode, self.settings)
            if episode["qa_enabled"] and not visual_qa:
                self.warn(
                    id,
                    "画面采用人工复核；仅执行媒体技术校验。"
                    if episode.get("creation_source")
                    else "未配置 VLM，视觉 QA 已跳过；仅执行媒体技术校验。",
                )
            if "reference_image" not in image["bindings"]:
                self.warn(id, REFERENCE_INPUT_WARNING)
            elif REFERENCE_INPUT_WARNING in episode.get("warnings", []):
                self.store.update(
                    "episode", id, lambda current: reconcile_reference_warning(current, image)
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
                    f"prop:{p['id']}",
                    "PROP_REFERENCE",
                    p["description"] + ". " + ", ".join(p["distinguishing_features"]),
                )
                for p in episode["bible"].get("props", [])
            ]
            descriptions += [
                (
                    "environment",
                    "ENVIRONMENT_REFERENCE",
                    episode["bible"]["environment"],
                ),
                (
                    "style",
                    "STYLE_REFERENCE",
                    episode["bible"]["style"],
                ),
            ]
            for key, type, prompt in descriptions:
                current = self.store.get("episode", id)
                if key in current["references"]:
                    continue
                result = await self.render(
                    episode,
                    reference_profile,
                    type,
                    {
                        **budget,
                        "prompt": anchored_prompt(
                            episode["bible"], reference_description(key, prompt), {}
                        ),
                        "negative": episode["bible"].get(
                            "negative_prompt", "text, watermark, artifacts"
                        )
                        if "negative" in reference_profile["bindings"]
                        else "",
                        "camera_motion": episode["bible"].get(
                            "camera_motion", "static reference view"
                        ),
                        "motion_strength": episode["bible"].get("motion_strength", 0.2),
                        "seed": episode["seed"] + len(current["references"]),
                    },
                    {},
                    "reference:" + key,
                )
                references = {**current["references"], key: result["id"]}
                self.store.update("episode", id, {"references": references})
                if (self.store.get("episode", id).get("render_recovery") or {}).get(
                    "shot_id"
                ) is None:
                    self.clear_recovery(id)
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
                    if previous and shot["transition_from_previous"] in CONTINUOUS:
                        previous = await self.ensure_full_tail(id, previous)
                    await self.generate_shot(
                        episode,
                        shot,
                        previous,
                        continuity,
                        image,
                        video,
                        budget,
                        agents,
                        visual_qa_enabled(self.store.get("episode", id), self.settings),
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
                            job["profile_snapshot"].get(
                                "media_type", job["profile_snapshot"].get("type")
                            )
                            == "image"
                            for job in jobs
                        ),
                        "video_generations": sum(
                            job["profile_snapshot"].get(
                                "media_type", job["profile_snapshot"].get("type")
                            )
                            == "video"
                            for job in jobs
                        ),
                        "failed_jobs": sum(job["status"] == "FAILED" for job in jobs),
                    }
                },
            )
            if preview_complete:
                self.stage(id, "AWAITING_REVIEW", preview_approved_at=None)

    async def generate_shot(
        self, episode, shot, previous, continuity, image, video, budget, agents, visual_qa
    ):
        id, sid = episode["id"], shot["id"]
        optimization = shot.get("optimization_attempt") or {}
        advisory = episode.get("qa_policy", "strict") == "advisory" or (
            bool(optimization) and optimization["retry_version"] == shot["retry_version"]
        )
        strict_qa = visual_qa and not advisory
        needs_end = video["capability"] == WorkflowCapability.FIRST_LAST_TO_VIDEO
        frame_roles = ("start_frame", "end_frame") if needs_end else ("start_frame",)
        user_inputs = role_overrides(video, parameter_overrides(episode, video))
        image_inputs = role_overrides(image, parameter_overrides(episode, image))
        frame_changes = {
            f"{role}_asset_id": user_inputs[role]
            for role in frame_roles
            if role in user_inputs and not shot.get(f"{role}_asset_id")
        }
        if frame_changes:
            shot = self.update_shot(id, sid, **frame_changes)
        if not shot["prompts"]:
            self.stage(id, "PREPARING_PROMPTS")
            self.update_shot(id, sid, status="PREPARING_PROMPTS")
            prompts = await agents.shot(
                episode["bible"],
                shot,
                continuity,
                ai_parameters(image, video),
                idea=episode["idea"],
            )
            if "pending_static_end_frame" in shot:
                prompts.allow_static_end_frame = shot["pending_static_end_frame"]
            shot = self.update_shot(id, sid, prompts=prompts.model_dump(mode="json"))
        prompts = shot["prompts"]
        recovery = self.store.get("episode", id).get("render_recovery") or {}
        cursor = recovery.get("cursor") if recovery.get("shot_id") == sid else None
        pending_retry = shot.get("qa_retry") or {}
        if not cursor and pending_retry and not pending_retry.get("pending"):
            saved_cursor = shot.get("render_cursor") or {}
            if saved_cursor.get("retry_version") == shot.get("retry_version"):
                # A cancellation may occur after a corrected job completes but before
                # its asset pointer is saved. Resume that attempt instead of redrawing it.
                cursor = saved_cursor
        recheck_legacy_frames = (
            not cursor
            and not pending_retry
            and (shot.get("error") or {}).get("code") in {"QA_FAILED", "KEYFRAMES_TOO_SIMILAR"}
            and not shot.get("video_asset_id")
            and all(shot.get(f"{role}_asset_id") for role in frame_roles)
        )
        if not cursor and (
            pending_retry
            or (shot.get("error") or {}).get("code")
            in {"QA_FAILED", "KEYFRAMES_TOO_SIMILAR", "VIDEO_TOO_SHORT"}
        ):
            # Continue is a fresh attempt after rejected output, not a replay of it.
            # Legacy failures retain their frames for structured re-inspection first.
            retry_updates = consume_retry(shot)
            for role in frame_roles:
                field = f"{role}_asset_id"
                if role in user_inputs and field in retry_updates:
                    retry_updates[field] = user_inputs[role]
            shot = self.update_shot(
                id,
                sid,
                **retry_updates,
                retry_version=shot.get("retry_version", 0) + 1,
                seed_offset=(shot.get("seed_offset", 0) + 10000) % 2147483648,
                error=None,
            )
        require_continuity(self.store.get("episode", id), image)
        ref_inputs = ContinuityManager.references(
            episode,
            shot,
            image,
            (previous or {}).get("actual_end_frame_asset_id")
            if shot["transition_from_previous"] in CONTINUOUS
            else None,
        )
        video_inputs = ContinuityManager.video_reference(previous, video)
        if previous and shot["transition_from_previous"] == "CONTINUE_VIDEO" and not video_inputs:
            self.warn(id, "视频工作流不支持 reference_video，CONTINUE_VIDEO 已降级为帧连续。")
        picture_hints = ""
        if image.get("capabilities", {}).get("supports_multi_reference"):
            names = {asset: role for role, asset in episode["references"].items()}
            picture_hints = "\n" + "\n".join(
                f"<Picture {i + 1}> supplies {names.get(asset, 'the previous shot state')}; use only the requested visual traits, not its composition."
                for i, asset in enumerate(ref_inputs.values())
            )
        base = {
            **budget,
            "seed": (episode["seed"] + shot["index"] * 100 + shot.get("seed_offset", 0))
            % 2147483648,
            "negative": prompts["negative_prompt"],
            "motion_strength": prompts.get("motion_strength", 0.6),
            "camera_motion": prompts.get("camera_motion", "static"),
        }
        retry_scope = "keyframes"
        retries = {"remaining": budget["max_retries"]}
        start_attempt = 0
        attempt_limit = budget["max_retries"] + int(recheck_legacy_frames)
        if cursor:
            base = deepcopy(cursor["base"])
            start_attempt = cursor["attempt"]
            retries["remaining"] = min(cursor["remaining"], budget["max_retries"])
            retry_scope = "video" if recovery.get("skip_keyframe_qa") else cursor["retry_scope"]
            attempt_limit = max(attempt_limit, cursor.get("attempt_limit", start_attempt))
        for attempt in range(start_attempt, attempt_limit + 1):
            self.check_cancel(id)
            shot = self.shot(id, sid)
            self.update_shot(
                id,
                sid,
                render_cursor={
                    "attempt": attempt,
                    "attempt_limit": attempt_limit,
                    "retry_version": shot["retry_version"],
                    "remaining": retries["remaining"],
                    "base": deepcopy(base),
                    "retry_scope": retry_scope,
                },
            )
            stamp = f"shot:{sid}:r{shot['retry_version']}:a{attempt}"
            try:
                self.stage(id, "GENERATING_KEYFRAMES")
                if not shot["start_frame_asset_id"]:
                    repairing_start = "start_frame" in (shot.get("qa_retry") or {}).get(
                        "failed_frames", []
                    )
                    if (
                        previous
                        and shot["transition_from_previous"] in CONTINUOUS
                        and not repairing_start
                    ):
                        shot = self.update_shot(
                            id,
                            sid,
                            start_frame_asset_id=previous["actual_end_frame_asset_id"],
                            reference_selection={
                                "source": "previous_actual_end",
                                "bindings": {"start_frame": previous["actual_end_frame_asset_id"]},
                            },
                            status="START_FRAME_READY",
                        )
                    else:
                        self.update_shot(id, sid, status="GENERATING_START_FRAME")
                        repair_reference = correction_reference(
                            image, shot, "start_frame", image_inputs
                        )
                        start_references = (
                            {**ref_inputs, "reference_image": repair_reference}
                            if repair_reference
                            else ref_inputs
                        )
                        candidate_results = []
                        for candidate in range(budget["candidates"] if strict_qa else 1):
                            result = await self.render(
                                episode,
                                image,
                                "SHOT_START_FRAME",
                                {
                                    **base,
                                    "seed": base["seed"] + attempt * 7 + candidate,
                                    "prompt": corrected_prompt(
                                        anchored_prompt(
                                            episode["bible"],
                                            prompts["start_frame_prompt"] + picture_hints,
                                            continuity,
                                            stage="start_frame",
                                            visual_continuity=prompts.get("visual_continuity"),
                                        ),
                                        shot,
                                        "start_frame",
                                        editing_rejected=bool(repair_reference),
                                    ),
                                },
                                start_references,
                                stamp + f":start:{candidate}",
                                sid,
                            )
                            score = 0
                            if strict_qa and budget["candidates"] > 1:
                                candidate_qa = await agents.qa(
                                    episode["bible"],
                                    shot,
                                    [self.assets.path(result["id"])],
                                    "start_candidate",
                                )
                                self.record_qa(
                                    id, sid, "start_candidate", candidate_qa, [result["id"]]
                                )
                                score = candidate_qa.score()
                            candidate_results.append((score, result["id"]))
                        best = max(candidate_results, key=lambda item: item[0])[1]
                        source_job = next(
                            j
                            for j in self.store.list("job", id)
                            if best in j.get("output_asset_ids", [])
                        )
                        shot = self.update_shot(
                            id,
                            sid,
                            start_frame_asset_id=best,
                            status="START_FRAME_READY",
                            reference_selection={
                                "source": "render_job",
                                "job_id": source_job["id"],
                                "workflow_id": source_job["workflow_id"],
                                "bindings": {
                                    role: asset
                                    for role, asset in source_job["asset_bindings"].items()
                                    if role in source_job["profile_snapshot"]["bindings"]
                                },
                            },
                        )
                if needs_end and not shot["end_frame_asset_id"]:
                    self.update_shot(id, sid, status="GENERATING_END_FRAME")
                    repair_reference = correction_reference(image, shot, "end_frame", image_inputs)
                    result = await self.render(
                        episode,
                        image,
                        "SHOT_END_FRAME",
                        {
                            **base,
                            "seed": base["seed"] + 1 + attempt * 7,
                            "prompt": corrected_prompt(
                                anchored_prompt(
                                    episode["bible"],
                                    prompts["end_frame_prompt"]
                                    + (
                                        "\n<Picture 1> is the approved shot start; edit it toward the requested end. "
                                        + "\n".join(picture_hints.splitlines()[2:])
                                        if picture_hints
                                        else ""
                                    ),
                                    {},
                                    stage="end_frame",
                                    visual_continuity=prompts.get("visual_continuity"),
                                ),
                                shot,
                                "end_frame",
                                editing_rejected=bool(repair_reference),
                            ),
                        },
                        {
                            **ref_inputs,
                            "reference_image": repair_reference or shot["start_frame_asset_id"],
                        },
                        stamp + ":end",
                        sid,
                    )
                    shot = self.update_shot(
                        id, sid, end_frame_asset_id=result["id"], status="KEYFRAMES_READY"
                    )
                else:
                    shot = self.update_shot(id, sid, status="KEYFRAMES_READY")
                if (
                    needs_end
                    and not shot["video_asset_id"]
                    and not (recovery.get("shot_id") == sid and recovery.get("skip_keyframe_qa"))
                    and not prompts.get("allow_static_end_frame", False)
                ):
                    comparison = await inspect_keyframes(
                        self.assets.path(shot["start_frame_asset_id"]),
                        self.assets.path(shot["end_frame_asset_id"]),
                    )
                    self.update_shot(
                        id,
                        sid,
                        keyframe_comparison={
                            **comparison,
                            "start_frame_asset_id": shot["start_frame_asset_id"],
                            "end_frame_asset_id": shot["end_frame_asset_id"],
                        },
                    )
                    if comparison["near_duplicate"] and advisory:
                        self.note_review(
                            id,
                            sid,
                            "keyframes",
                            "KEYFRAMES_TOO_SIMILAR",
                            "首尾帧几乎相同，视频可能缺少预期变化，请复核镜头动作。",
                            [shot["start_frame_asset_id"], shot["end_frame_asset_id"]],
                        )
                    elif comparison["near_duplicate"]:
                        retry_scope = "transition"
                        raise AppError(
                            "KEYFRAMES_TOO_SIMILAR",
                            f"第 {shot['index'] + 1} 镜首尾帧几乎相同，已暂停视频生成。"
                            "请检查尾帧变化描述、参考图设置及 seed 绑定；"
                            "有意定格的镜头可在分镜预览或重跑选项中允许静止首尾帧。",
                            {
                                "shot_id": sid,
                                **comparison,
                                "failed_frames": ["end_frame"],
                                "frame_corrections": {
                                    "end_frame": "The previous output duplicated the start frame. "
                                    "Show the visible change specified by the requested ending state; "
                                    "preserve framing when the target calls for a locked camera."
                                },
                            },
                        )
                if strict_qa and not shot["video_asset_id"] and retry_scope != "video":
                    keyframes = [shot[f"{role}_asset_id"] for role in frame_roles]
                    qa = await agents.qa(
                        episode["bible"],
                        shot,
                        [self.assets.path(asset_id) for asset_id in keyframes],
                        "keyframes",
                    )
                    scope = qa.retry_scope(high=episode["quality"] == "high")
                    if qa.failed_frames:
                        scope = "keyframes"
                    self.record_qa(
                        id,
                        sid,
                        "keyframes",
                        qa,
                        keyframes,
                    )
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
                            {
                                "scope": retry_scope,
                                "explanation": qa.explanation,
                                "failed_frames": qa.failed_frames
                                or failed_frames(retry_scope, needs_end),
                                "frame_corrections": qa.frame_corrections,
                            },
                        )
                if not shot["video_asset_id"]:
                    self.stage(id, "RENDERING_VIDEO")
                    self.update_shot(id, sid, status="RENDERING_VIDEO")
                    result = await self.render_video(
                        episode, shot, image, video, base, ref_inputs, video_inputs, stamp, retries
                    )
                    shot = self.update_shot(
                        id,
                        sid,
                        video_asset_id=result["id"],
                        actual_duration=media_duration(result["metadata"]),
                        status="VIDEO_READY",
                    )
                if shot.get("actual_duration") is None:
                    cached = self.store.get("asset", shot["video_asset_id"])
                    shot = self.update_shot(
                        id, sid, actual_duration=media_duration(cached["metadata"])
                    )
                current = self.store.get("episode", id)
                if visual_qa_enabled(current, self.settings):
                    await self.review_video(current, shot, previous, agents, advisory=advisory)
                elif advisory and current["qa_enabled"]:
                    self.note_review(
                        id,
                        sid,
                        "video",
                        "CONFIGURATION_REQUIRED",
                        "视频仅通过媒体技术检查，请人工复核画面。",
                        [shot["video_asset_id"]],
                    )
                shot = await self.ensure_full_tail(id, shot)
                notes = current_review_notes(self.shot(id, sid))
                self.update_shot(
                    id,
                    sid,
                    status="PASSED",
                    needs_review=bool(notes),
                    review_notes=notes,
                    actual_end_frame_asset_id=shot["actual_end_frame_asset_id"],
                    continuity_after={**continuity, **prompts["continuity_state"]},
                    error=None,
                    qa_retry=None,
                    qa_frame_corrections={},
                )
                self.clear_recovery(id, sid)
                return
            except AppError as exc:
                repair_frames = None
                inspecting_old_frames = (
                    recheck_legacy_frames
                    and attempt == start_attempt
                    and exc.code in {"QA_FAILED", "KEYFRAMES_TOO_SIMILAR"}
                    and not self.shot(id, sid).get("video_asset_id")
                )
                if exc.code in {"QA_FAILED", "KEYFRAMES_TOO_SIMILAR", "VIDEO_TOO_SHORT"}:
                    if exc.code == "VIDEO_TOO_SHORT":
                        retry_scope = "video"
                    details = exc.details or {}
                    retry_scope = details.get("scope", retry_scope)
                    repair_frames = details.get("failed_frames") or failed_frames(
                        retry_scope, needs_end
                    )
                    shot = self.shot(id, sid)
                    corrections = dict(shot.get("qa_frame_corrections") or {})
                    for role in repair_frames:
                        correction = details.get("frame_corrections", {}).get(role)
                        if not correction:
                            correction = details.get("explanation", "")[:1500]
                        if correction:
                            corrections[role] = correction
                    self.update_shot(
                        id,
                        sid,
                        qa_retry=retry_state(shot, retry_scope, repair_frames),
                        qa_frame_corrections=corrections,
                    )
                    # The recovered submission is settled now; future QA attempts are new work.
                    self.clear_recovery(id, sid)
                    recovery = {}
                    locked = [role for role in repair_frames if role in user_inputs]
                    if locked:
                        raise AppError(
                            exc.code,
                            exc.message + "；失败帧已由高级素材覆盖固定，请更换该素材后再继续。",
                            {**details, "locked_frames": locked},
                        ) from exc
                if (
                    exc.code
                    not in {
                        "QA_FAILED",
                        "OUT_OF_MEMORY",
                        "VIDEO_TOO_SHORT",
                        "KEYFRAMES_TOO_SIMILAR",
                    }
                    or (retries["remaining"] == 0 and not inspecting_old_frames)
                    or (exc.code == "KEYFRAMES_TOO_SIMILAR" and "end_frame" in user_inputs)
                ):
                    raise
                if not inspecting_old_frames:
                    retries["remaining"] -= 1
                if exc.code == "VIDEO_TOO_SHORT":
                    retry_scope = "video"
                if exc.code == "OUT_OF_MEMORY":
                    retry_scope = (
                        "video"
                        if all(self.shot(id, sid)[f"{role}_asset_id"] for role in frame_roles)
                        else "keyframes"
                    )
                    base.update(
                        width=max(256, (base["width"] * 3 // 4 // 16) * 16),
                        height=max(256, (base["height"] * 3 // 4 // 16) * 16),
                        _oom_recovery=True,
                    )
                    self.warn(id, "检测到显存不足，降低生成分辨率后重试，目标时间线保持不变。")
                shot = self.shot(id, sid)
                if repair_frames is not None:
                    self.update_shot(id, sid, **consume_retry(shot))
                    continue
                updates = {"video_asset_id": None, "actual_end_frame_asset_id": None}
                if retry_scope == "keyframes":
                    updates.update(start_frame_asset_id=None, end_frame_asset_id=None)
                elif retry_scope == "transition":
                    updates[f"{'end_frame' if needs_end else 'start_frame'}_asset_id"] = None
                self.update_shot(id, sid, **updates)
        raise AppError("QA_FAILED", "镜头超出重试预算")

    def clear_recovery(self, id, shot_id=None):
        current = self.store.get("episode", id)
        if not current.get("render_recovery") and not any(
            s["id"] == shot_id and s.get("render_cursor") for s in current["shots"]
        ):
            return

        def change(episode):
            recovery = episode.get("render_recovery")
            if recovery and recovery.get("shot_id") == shot_id:
                episode.pop("render_recovery", None)
            if shot_id:
                next(s for s in episode["shots"] if s["id"] == shot_id).pop("render_cursor", None)

        self.store.update("episode", id, change)

    async def render_video(
        self, episode, shot, image, video, base, references, video_inputs, stamp, retries
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
                stage="video",
                visual_continuity=shot["prompts"].get("visual_continuity"),
            ),
        }
        inputs = {
            **video_inputs,
            "start_frame": shot["start_frame_asset_id"],
            **references,
        }
        if video["capability"] == WorkflowCapability.FIRST_LAST_TO_VIDEO:
            inputs["end_frame"] = shot["end_frame_asset_id"]
        try:
            return await self.render(
                episode, video, "SHOT_VIDEO", values, inputs, stamp + ":video", sid
            )
        except AppError as exc:
            if exc.code != "OUT_OF_MEMORY" or retries["remaining"] == 0:
                raise
            retries["remaining"] -= 1
        smaller = {
            **values,
            "_oom_recovery": True,
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
            if exc.code != "OUT_OF_MEMORY" or retries["remaining"] == 0:
                raise
            retries["remaining"] -= 1
        effective_prompt = role_overrides(video, parameter_overrides(episode, video)).get(
            "prompt", shot["prompts"]["video_prompt"]
        )
        # Each temporal segment would otherwise speak the same whole sentence again.
        spoken_audio = video["capabilities"].get("audio_prompt_format") == H3 and SPEECH.search(
            effective_prompt
        )
        alternative = video["capabilities"].get("low_memory_workflow_id")
        if alternative:
            video = self.router.select("video", alternative)
        if spoken_audio:
            if (
                alternative
                and video["capabilities"].get("audio_prompt_format") == H3
                and duration <= video["capabilities"]["max_duration"]
                and (
                    video["capability"] != WorkflowCapability.FIRST_LAST_TO_VIDEO
                    or inputs.get("end_frame")
                )
            ):
                if video["capability"] == WorkflowCapability.IMAGE_TO_VIDEO:
                    inputs.pop("end_frame", None)
                return await self.render(
                    episode,
                    video,
                    "SHOT_VIDEO",
                    smaller,
                    inputs,
                    stamp + ":video:audio-fallback",
                    sid,
                )
            raise AppError(
                "OUT_OF_MEMORY",
                "含台词的视频降分辨率后仍显存不足；为避免分段重复台词，请降低尺寸或配置支持原生声音且容纳整镜的低显存工作流",
            )
        max_segment = min(2, video["capabilities"]["max_duration"])
        count = math.ceil(duration / max_segment)
        if count == 1 and not alternative:
            raise AppError("OUT_OF_MEMORY", "最小批次与降分辨率仍无法渲染，请配置低显存视频工作流")
        if duration / count < 1:
            raise AppError("OUT_OF_MEMORY", "无法在最短 1 秒限制内继续分段")
        segments, start = [], shot["start_frame_asset_id"]
        needs_end = video["capability"] == WorkflowCapability.FIRST_LAST_TO_VIDEO
        inputs.pop("end_frame", None)
        for index in range(count):
            length = duration / count
            end = shot["end_frame_asset_id"]
            if needs_end and (index < count - 1 or not end):
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
                {
                    **smaller,
                    "duration": length,
                    "seed": base["seed"] + index,
                    "prompt": anchored_prompt(
                        episode["bible"],
                        segment_prompt(
                            shot["prompts"]["video_prompt"],
                            duration,
                            index * length,
                            min(duration, (index + 1) * length),
                        ),
                        shot["prompts"]["continuity_state"],
                        stage="video",
                    ),
                },
                {**inputs, "start_frame": start, **({"end_frame": end} if needs_end else {})},
                stamp + f":segment:{index}",
                sid,
            )
            segments.append(self.assets.path(result["id"]))
            if index < count - 1:
                tail = self.assets.allocate(id, ".png")
                await extract_frame(self.assets.path(result["id"]), tail)
                actual = await self.assets.register(tail, id, "SEGMENT_END_FRAME", sid)
                start = actual["id"]
        output = self.assets.allocate(id, ".mp4")
        await compose(segments, output, base["width"], base["height"], base["fps"])
        return await self.assets.register(output, id, "SHOT_VIDEO", sid)

    async def ensure_full_tail(self, id: str, shot: dict) -> dict:
        if (
            shot.get("actual_end_frame_asset_id")
            and shot.get("full_tail_video_asset_id") == shot["video_asset_id"]
        ):
            return shot
        last = self.assets.allocate(id, ".png")
        await extract_frame(self.assets.path(shot["video_asset_id"]), last)
        end = await self.assets.register(last, id, "ACTUAL_END_FRAME", shot["id"])
        return self.update_shot(
            id,
            shot["id"],
            actual_end_frame_asset_id=end["id"],
            full_tail_video_asset_id=shot["video_asset_id"],
        )

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
            [self.assets.path(shot["video_asset_id"]) for shot in shots],
            output,
            budget["width"],
            budget["height"],
            budget["fps"],
        )
        self.check_cancel(id)
        asset = await self.assets.register(output, id, "FINAL_VIDEO")

        def finish(current):
            if current["status"] == "CANCELLED":
                raise AppError("CANCELLED", "单集已取消")
            old = current.get("final_video_asset_id")
            if old:
                current.setdefault("rerun_history", []).append(
                    {
                        "id": uid(),
                        "created_at": now(),
                        "scope": "compose",
                        "shot_ids": [s["id"] for s in shots],
                        "affected_shot_ids": [],
                        "new_seed": False,
                        "final_video_asset_id": old,
                        "final_duration": current.get("final_duration"),
                        "shots": deepcopy(current["shots"]),
                    }
                )
            durations = dict(
                zip((s["id"] for s in shots), metadata["source_durations"], strict=True)
            )
            for shot in current["shots"]:
                if shot["id"] in durations:
                    shot["actual_duration"] = durations[shot["id"]]
            current.update(
                status="COMPLETED",
                final_video_asset_id=asset["id"],
                completed_at=now(),
                final_duration=metadata["duration"],
                composition_policy="full_clips",
                error=None,
            )

        self.store.update("episode", id, finish)
