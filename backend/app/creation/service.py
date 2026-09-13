"""One creation contract for file imports, HTTP and MCP. No model or network calls."""

import hashlib
import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from app.agents.audio import audio_output
from app.agents.parameters import constrained_output
from app.agents.schemas import ShotPlan, ShotPrompts, VisualBible
from app.agents.timing import read_timing
from app.core.errors import AppError
from app.creation.schemas import CreationPackage
from app.generation.continuity import continuity_report, require_continuity
from app.generation.parameters import ai_parameters
from app.generation.preview import rebind_story
from app.generation.schemas import EpisodeRerun


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def identity(*parts):
    return str(uuid5(NAMESPACE_URL, "autodirector:" + ":".join(map(str, parts))))


def optional(store, kind, id):
    try:
        return store.get(kind, id)
    except AppError as exc:
        if exc.code != "NOT_FOUND":
            raise
        return None


def summary(record):
    return {key: record[key] for key in ("id", "title", "revision", "updated_at", "created_at")}


class CreationService:
    def __init__(self, generation):
        self.generation = generation
        self.store = generation.store

    def projects(self):
        return [summary(item) for item in self.store.list("creation_project")]

    def revision(self, project_id, revision=None):
        project = self.store.get("creation_project", project_id)
        number = project["revision"] if revision is None else revision
        return self.store.get("creation_revision", identity(project_id, number))

    def detail(self, project_id):
        project = self.store.get("creation_project", project_id)
        revisions = self.store.list("creation_revision", project_id)
        return {
            **summary(project),
            "document": self.revision(project_id, project["revision"])["document"],
            "history": [
                {key: item[key] for key in ("revision", "content_hash", "created_at")}
                for item in revisions
            ],
            "productions": self.productions(project_id),
        }

    def productions(self, project_id):
        self.store.get("creation_project", project_id)
        return [
            {
                "episode_id": episode["id"],
                "revision": episode["creation_source"]["revision"],
                "title": episode["title"],
                "status": episode["status"],
                "version": episode["version"],
                "final_video_asset_id": episode["final_video_asset_id"],
            }
            for episode in self.store.list("episode")
            if (episode.get("creation_source") or {}).get("project_id") == project_id
        ]

    def _receipt(self, store, project_id, request_id, body):
        id = identity(project_id, "request", request_id)
        previous = optional(store, "creation_request", id)
        fingerprint = digest(body)
        if previous and previous["fingerprint"] != fingerprint:
            raise AppError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同内容", status=409)
        return id, fingerprint, previous

    def _save_version(self, store, project_id, number, document):
        data = document.model_dump(mode="json")
        # Also bounds arbitrary dictionary values and individual creative notes.
        if len(json.dumps(data, ensure_ascii=False).encode()) > 1_500_000:
            raise AppError("PAYLOAD_TOO_LARGE", "创作包须小于 1.5 MB", status=413)
        fingerprint = digest(data)
        store.create(
            "creation_revision",
            {"revision": number, "document": data, "content_hash": fingerprint},
            id=identity(project_id, number),
            parent=project_id,
        )
        return {"project_id": project_id, "revision": number, "content_hash": fingerprint}

    def create(self, request):
        project_id = identity("creation", request.request_id)

        def write(store):
            key, fingerprint, previous = self._receipt(
                store, project_id, request.request_id, request.model_dump(mode="json")
            )
            if previous:
                return previous["result"]
            store.create(
                "creation_project",
                {
                    "title": request.document.title or request.document.brief.idea[:80],
                    "revision": 1,
                },
                id=project_id,
            )
            result = self._save_version(store, project_id, 1, request.document)
            store.create(
                "creation_request",
                {"fingerprint": fingerprint, "result": result},
                id=key,
                parent=project_id,
            )
            return result

        return self.store.atomic(write)

    def save(self, project_id, request):
        def write(store):
            key, fingerprint, previous = self._receipt(
                store, project_id, request.request_id, request.model_dump(mode="json")
            )
            if previous:
                return previous["result"]
            project = store.get("creation_project", project_id)
            if project["revision"] != request.expected_revision:
                raise AppError(
                    "CONFLICT",
                    "创作包已有新版本，请读取并合并后重新保存",
                    {"current_revision": project["revision"]},
                    status=409,
                )
            number = project["revision"] + 1
            result = self._save_version(store, project_id, number, request.document)
            store.update(
                "creation_project",
                project_id,
                {
                    "revision": number,
                    "title": request.document.title or request.document.brief.idea[:80],
                },
            )
            store.create(
                "creation_request",
                {"fingerprint": fingerprint, "result": result},
                id=key,
                parent=project_id,
            )
            return result

        return self.store.atomic(write)

    def _profiles(self, document):
        episode = self.generation.draft(document.brief.episode_request())
        profiles = self.generation.preview_profiles(episode)
        return episode, profiles

    def _constraints(self, document):
        episode, profiles = self._profiles(document)
        rebind_story(episode, *profiles)
        return {
            "workflow_configuration": [
                {
                    "id": profile["id"],
                    "name": profile["name"],
                    "configuration_version": profile.get(
                        "configuration_version", profile["version"]
                    ),
                    "capability": profile["capability"],
                    "capabilities": profile["capabilities"],
                }
                for profile in profiles
            ],
            "budget": episode["budget"],
            "ai_parameters": ai_parameters(*profiles),
            "rules": [
                "镜头 index 从 0 连续排列；id 在版本间保持稳定且不重复。",
                "总时长必须与 brief.target_duration 一致；单镜时长保留最多两位小数。",
                "首镜不能延续不存在的前镜；画面提示词只描述本镜。",
                "新镜头填写 prompts.visual_continuity：scene_id、出场角色 ID、有序 reference_roles、景别及 state_in/state_out；第一项是单参考工作流的实际输入。",
                "参考用 character:<Bible ID>、prop:<Bible props ID>、environment 或 style；重复出现的道具建立 bible.props，插入特写优先选 prop。视觉设定只写外观，style 的声音/表演字段不用于参考图。状态键和值在同场景跨反打复用；仅有意跳转时填写 intentional_jump 原因。",
                "完整包须包含 title、logline、bible 和每镜 prompts；草稿可缺项。",
                "ai_parameters 只填写导出的 AI owner 字段，prompt 由阶段提示词提供。",
                "H3 声音格式由 audio_prompt_format 明确指定；视频提示词是实际声音执行来源。",
                "校验是制作技术检查，不代表剧情或画面质量已经通过人工审核。",
            ],
            "bible_schema": constrained_output(
                VisualBible, ai_parameters(profiles[2])
            ).model_json_schema(),
            "shot_prompts_schema": audio_output(
                constrained_output(ShotPrompts, ai_parameters(*profiles[:2])),
                ai_parameters(*profiles[:2]),
            ).model_json_schema(),
        }

    def context(self, project_id):
        revision = self.revision(project_id)
        document = CreationPackage.model_validate(revision["document"])
        try:
            constraints = self._constraints(document)
            constraints_hash, problem = digest(constraints), None
        except AppError as exc:
            constraints, constraints_hash, problem = None, None, exc.as_dict()
        return {
            "project_id": project_id,
            "revision": revision["revision"],
            "content_hash": revision["content_hash"],
            "document": revision["document"],
            "constraints": constraints,
            "constraints_hash": constraints_hash,
            "configuration_error": problem,
            "schema": CreationPackage.model_json_schema(),
        }

    def _snapshot(self, document):
        episode, profiles = self._profiles(document)
        if document.brief.visual_review == "model" and not self.generation.settings.vlm_model:
            raise AppError(
                "CONFIGURATION_REQUIRED",
                "请选择人工复核，或先在连接与设置中配置视觉模型",
                status=409,
            )
        if not document.title.strip() or not document.logline.strip():
            raise AppError(
                "PACKAGE_INCOMPLETE", "请补齐标题与故事梗概", {"field": "title/logline"}, 422
            )
        if document.bible is None or not document.shots:
            raise AppError(
                "PACKAGE_INCOMPLETE", "请补齐视觉设定与分镜", {"field": "bible/shots"}, 422
            )
        if [s.index for s in document.shots] != list(range(len(document.shots))):
            raise AppError(
                "PACKAGE_INVALID", "镜头 index 必须从 0 连续排列", {"field": "shots.index"}, 422
            )
        if len({s.id for s in document.shots}) != len(document.shots):
            raise AppError("PACKAGE_INVALID", "镜头 id 不能重复", {"field": "shots.id"}, 422)
        if document.shots[0].transition_from_previous in {"CONTINUE_FRAME", "CONTINUE_VIDEO"}:
            raise AppError(
                "PACKAGE_INVALID",
                "首镜不能延续前镜",
                {"field": "shots.0.transition_from_previous"},
                422,
            )
        ids = [c.id for c in document.bible.characters]
        if len(set(ids)) != len(ids):
            raise AppError(
                "PACKAGE_INVALID", "角色 id 不能重复", {"field": "bible.characters"}, 422
            )
        bible = constrained_output(VisualBible, ai_parameters(profiles[2])).model_validate(
            document.bible.model_dump()
        )
        shot_schema = audio_output(
            constrained_output(ShotPrompts, ai_parameters(*profiles[:2])),
            ai_parameters(*profiles[:2]),
        )
        shots = []
        for shot in document.shots:
            if shot.prompts is None:
                raise AppError(
                    "PACKAGE_INCOMPLETE",
                    f"第 {shot.index + 1} 镜缺少提示词",
                    {"shot_id": shot.id},
                    422,
                )
            if abs(shot.duration * 100 - round(shot.duration * 100)) > 1e-7:
                raise AppError(
                    "PACKAGE_INVALID", "镜头时长最多保留两位小数", {"shot_id": shot.id}, 422
                )
            try:
                prompts = shot_schema.model_validate(shot.prompts.model_dump()).model_dump(
                    mode="json"
                )
                read_timing(prompts["video_prompt"], shot.duration)
            except (ValidationError, ValueError) as exc:
                raise AppError(
                    "PACKAGE_INVALID",
                    f"第 {shot.index + 1} 镜提示词不符合约束",
                    {
                        "shot_id": shot.id,
                        "reason": str(exc)[:1500],
                    },
                    422,
                ) from exc
            shots.append(
                {
                    **shot.model_dump(mode="json", exclude={"prompts"}),
                    "prompts": prompts,
                    "enabled": True,
                    "status": "PENDING",
                    "start_frame_asset_id": None,
                    "end_frame_asset_id": None,
                    "video_asset_id": None,
                    "actual_end_frame_asset_id": None,
                    "qa": [],
                    "error": None,
                    "retry_version": 0,
                }
            )
        episode.update(
            title=document.title,
            plan={
                "title": document.title,
                "logline": document.logline,
                "target_duration": document.brief.target_duration,
                "shots": [{key: s[key] for key in ShotPlan.model_fields} for s in shots],
            },
            bible=bible.model_dump(mode="json"),
            shots=shots,
            creation_visual_review=document.brief.visual_review,
        )
        try:
            rebind_story(episode, *profiles)
        except AppError as exc:
            raise AppError("PACKAGE_INVALID", exc.message, exc.details, status=422) from exc
        require_continuity(episode, profiles[0])
        return episode

    def validate(self, project_id, revision=None):
        saved = self.revision(project_id, revision)
        document = CreationPackage.model_validate(saved["document"])
        constraints_hash = None
        try:
            constraints_hash = digest(self._constraints(document))
            episode = self._snapshot(document)
            report = continuity_report(episode, self._profiles(document)[1][0])
            issues = []
        except AppError as exc:
            issues = [exc.as_dict()]
        except (ValidationError, ValueError) as exc:
            issues = [{"code": "PACKAGE_INVALID", "message": str(exc)[:1500]}]
        return {
            "valid": not issues,
            "issues": issues,
            "warnings": report["warnings"] if not issues else [],
            "revision": saved["revision"],
            "content_hash": saved["content_hash"],
            "constraints_hash": constraints_hash,
            "review": "制作技术校验；剧情与画面需由用户复核",
        }

    def submit(self, project_id, request):
        def write(store):
            key, fingerprint, previous = self._receipt(
                store, project_id, request.request_id, {"submit": request.model_dump(mode="json")}
            )
            if previous:
                return previous["result"]
            project = store.get("creation_project", project_id)
            if project["revision"] != request.expected_revision:
                raise AppError("CONFLICT", "创作包已有新版本，请重新校验", status=409)
            saved = store.get("creation_revision", identity(project_id, request.expected_revision))
            document = CreationPackage.model_validate(saved["document"])
            if digest(self._constraints(document)) != request.constraints_hash:
                raise AppError(
                    "PREVIEW_STALE", "制作约束已变更，请获取上下文后重新校验", status=409
                )
            try:
                snapshot = self._snapshot(document)
            except ValidationError as exc:
                raise AppError("PACKAGE_INVALID", "创作包不符合制作约束", status=422) from exc
            episode_id = identity(project_id, "production", request.request_id)
            for shot in snapshot["shots"]:
                shot["creation_shot_id"] = shot["id"]
                shot["id"] = identity(episode_id, shot["id"])
            snapshot["creation_source"] = {
                "project_id": project_id,
                "revision": saved["revision"],
                "content_hash": saved["content_hash"],
                "constraints_hash": request.constraints_hash,
            }
            episode = store.create("episode", snapshot, id=episode_id)
            result = {
                "episode_id": episode_id,
                "version": episode["version"],
                "status": "AWAITING_REVIEW",
                "revision": saved["revision"],
            }
            store.create(
                "creation_request",
                {"fingerprint": fingerprint, "result": result},
                id=key,
                parent=project_id,
            )
            return result

        return self.store.atomic(write)

    def confirm(self, project_id, episode_id, request):
        self.feedback(project_id, episode_id)
        episode = self.generation.enqueue(
            episode_id,
            "approve",
            request.expected_version,
            request_id=str(request.request_id),
        )
        return {
            "episode_id": episode_id,
            "status": episode["status"],
            "version": episode["version"],
        }

    def feedback(self, project_id, episode_id):
        episode = self.generation.detail(episode_id)
        if (episode.get("creation_source") or {}).get("project_id") != project_id:
            raise AppError("NOT_FOUND", "此制作不属于当前创作项目", status=404)
        return {
            "episode_id": episode_id,
            "version": episode["version"],
            "status": episode["status"],
            "image_review_required": episode.get("image_review_required", False),
            "preproduction_pending": episode.get("preproduction_pending"),
            "continuity_report": episode.get("continuity_report"),
            "source": episode["creation_source"],
            "error": episode["error"],
            "final_video_asset_id": episode["final_video_asset_id"],
            "shots": [
                {
                    key: deepcopy(s.get(key))
                    for key in (
                        "id",
                        "creation_shot_id",
                        "title",
                        "duration",
                        "status",
                        "prompts",
                        "error",
                        "start_frame_asset_id",
                        "end_frame_asset_id",
                        "video_asset_id",
                        "qa",
                        "review_notes",
                        "reference_selection",
                        "continuity_review_status",
                        "actual_end_frame_asset_id",
                    )
                }
                for s in episode["shots"]
            ],
            "assets": [
                {"id": a["id"], "type": a["type"], "url": f"/api/v1/assets/{a['id']}/file"}
                for a in self.store.list("asset", episode_id)
            ],
            "jobs": [
                {key: j.get(key) for key in ("id", "status", "type", "progress", "error")}
                for j in self.store.list("job", episode_id)
            ],
        }

    def rerun(self, project_id, episode_id, request):
        self.feedback(project_id, episode_id)
        return self.generation.rerun(
            episode_id,
            EpisodeRerun.model_validate(request.model_dump(exclude={"request_id", "confirm"})),
            request_id=str(request.request_id),
        )

    async def cancel(self, project_id, episode_id):
        self.feedback(project_id, episode_id)
        await self.generation.cancel(episode_id)
        return self.feedback(project_id, episode_id)
