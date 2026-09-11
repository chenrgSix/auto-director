"""Review a persisted plan before any rendering; keep edits within the existing pipeline."""

from copy import deepcopy
from decimal import Decimal

from app.agents.directing import generation_budget
from app.agents.schemas import ShotPlan
from app.core.errors import AppError
from app.core.limits import MIN_SHOT_SECONDS
from app.generation.parameters import (
    duration_seconds,
    fit_budget_dimensions,
    parameter_overrides,
    resolve_parameters,
    role_overrides,
    usable_ai_values,
    validate_strategy,
)
from app.generation.workflow_state import scope_ai_parameters
from app.workflows.analyzer import check_value
from app.workflows.duration import duration_limits, render_maximum, uses_remote_video
from app.workflows.frame_timing import generation_fps

PROMPT_FIELDS = ("start_frame_prompt", "end_frame_prompt", "video_prompt")
PROMPT_LABELS = dict(zip(PROMPT_FIELDS, ("首帧提示词", "尾帧提示词", "视频提示词"), strict=True))


def workflow_versions(profiles):
    return {profile["id"]: profile["version"] for profile in profiles}


def require_current_preview(episode, profiles):
    if (episode.get("preview") or {}).get("workflow_versions") != workflow_versions(profiles):
        raise AppError("PREVIEW_STALE", "工作流配置已变更，请更新分镜预览后确认", status=409)


def preview_video(episode, video):
    snapshot = episode.get("preview") or {}
    if (
        "remote_video" not in video
        and snapshot.get("workflow_versions", {}).get(video["id"]) == video["version"]
        and snapshot.get("video_parameter_snapshot") is not None
    ):
        return {**video, "parameters": deepcopy(snapshot["video_parameter_snapshot"])}
    return video


def prompt_view(episode, shot, image, video):
    """Show effective prompt inputs, including AI mappings and fixed user overrides."""
    values, locked, hints = {}, {}, {}
    for field in PROMPT_FIELDS:
        profile = video if field == "video_prompt" else image
        role = "prompt"
        parameters = [p for p in profile["parameters"] if p.get("role") == role]
        generated = shot["prompts"].get("ai_parameters", {}).get(profile["id"], {})
        usable_ai_values(profile, generated, stage_prompt=True)
        value = shot["prompts"][field]
        duplicates = [generated[p["key"]] for p in parameters if p["key"] in generated]
        if duplicates:
            reason = (
                "AI 动态提示词为空"
                if all(not v.strip() for v in duplicates)
                else "旧动态提示词重复"
            )
            hints[field] = f"{reason}，已使用本镜的{PROMPT_LABELS[field]}。"
        overrides = role_overrides(profile, parameter_overrides(episode, profile))
        values[field] = overrides.get(role, value)
        if not parameters or any(
            not p["editable"] or p["override_policy"] == "never" for p in parameters
        ):
            locked[field] = "工作流未开放此提示词编辑"
        if role in overrides:
            locked[field] = "使用创建时的高级固定覆盖"
            hints.pop(field, None)
        asset_role = "end_frame" if field == "end_frame_prompt" else "start_frame"
        video_overrides = role_overrides(video, parameter_overrides(episode, video))
        if field != "video_prompt" and asset_role in video_overrides:
            locked[field] = "使用指定素材，不生成此关键帧"
        if (
            field == "start_frame_prompt"
            and shot["index"] > 0
            and shot["transition_from_previous"] in {"CONTINUE_FRAME", "CONTINUE_VIDEO"}
        ):
            locked[field] = "延续上镜实际尾帧，不单独生成首帧"
        if field == "end_frame_prompt" and video["capability"] == "IMAGE_TO_VIDEO":
            locked[field] = "此工作流仅使用首帧"
    return {"values": values, "locked": locked, "hints": hints}


def validate_review_prompts(episode, image, video):
    for shot in episode["shots"]:
        view = prompt_view(episode, shot, image, video)
        for field in PROMPT_FIELDS:
            if field in view["locked"]:
                continue
            value = view["values"][field]
            if not isinstance(value, str) or not value.strip():
                raise AppError(
                    "PREVIEW_INVALID",
                    f"第 {shot['index'] + 1} 镜「{shot['title']}」的{PROMPT_LABELS[field]}不能为空",
                    {"shot_id": shot["id"], "field": field},
                )


def refresh_timing_limits(episode, video):
    """Refresh legacy duration ceilings without changing prepared content or image sizes."""
    video = preview_video(episode, video)
    budget = episode["budget"]
    roles = role_overrides(video, parameter_overrides(episode, video))
    budget["fps"] = generation_fps(video, budget["fps"], roles.get("fps"))
    budget.update(duration_limits(episode, video["capabilities"]))
    budget["render_max_duration"] = render_maximum(video, budget)
    budget["max_duration"] = min(budget["max_duration"], budget["render_max_duration"])
    if episode.get("preview"):
        episode["preview"].update(
            {key: budget[key] for key in ("max_duration", "render_max_duration")}
        )


def validate_timing(episode, video):
    video = preview_video(episode, video)
    refresh_timing_limits(episode, video)
    fit_budget_dimensions(episode, video, episode["budget"])
    snapshot = episode.get("preview") or {}
    if (
        "remote_video" not in video
        and "remote_video" in snapshot
        and snapshot.get("workflow_versions", {}).get(video["id"]) == video["version"]
    ):
        video = {**video, "remote_video": snapshot["remote_video"]}
    shots = episode["shots"]
    total = sum((Decimal(str(s["duration"])) for s in shots), Decimal(0))
    if abs(total - Decimal(str(episode["target_duration"]))) > Decimal("0.005"):
        raise AppError("PREVIEW_INVALID", "各镜时长之和必须等于短片总时长")
    for shot in shots:
        resolve_parameters(
            video,
            {**episode["budget"], "duration": shot["duration"]},
            {},
            parameter_overrides(episode, video),
            episode.get("advanced_mode", False),
            episode["budget"],
        )


def prepare_review(episode, image, video, reference):
    for shot in episode["shots"]:
        shot["preview_prompt_view"] = prompt_view(episode, shot, image, video)
    episode["preview"] = {
        "workflow_versions": workflow_versions((image, video, reference)),
        "capability": video["capability"],
        "min_duration": MIN_SHOT_SECONDS,
        "max_duration": episode["budget"]["max_duration"],
        "fixed_duration": episode.get("fixed_shot_duration"),
        "render_max_duration": episode["budget"]["render_max_duration"],
        "remote_video": uses_remote_video(video),
        "video_parameter_snapshot": deepcopy(video["parameters"]),
    }
    validate_timing(episode, video)
    validate_review_prompts(episode, image, video)


def apply_edits(episode, request, image, video):
    if [item.id for item in request.shots] != [shot["id"] for shot in episode["shots"]]:
        raise AppError("PREVIEW_INVALID", "请保留当前全部镜头及顺序")
    for shot, item in zip(episode["shots"], request.shots, strict=True):
        view = prompt_view(episode, shot, image, video)
        edited = set(shot.get("preview_edited_fields", []))
        for field in PROMPT_FIELDS:
            value = getattr(item, field)
            if value == view["values"][field]:
                continue
            if not value.strip():
                raise AppError(
                    "PREVIEW_INVALID",
                    f"第 {shot['index'] + 1} 镜「{shot['title']}」的{PROMPT_LABELS[field]}不能为空",
                    {"shot_id": shot["id"], "field": field},
                )
            if field in view["locked"]:
                raise AppError("OVERRIDE_NOT_ALLOWED", view["locked"][field])
            profile = video if field == "video_prompt" else image
            for parameter in profile["parameters"]:
                if parameter.get("role") == "prompt":
                    check_value(parameter, value)
            shot["prompts"][field] = value
            edited.add(field)
        if not item.title.strip():
            raise AppError("PREVIEW_INVALID", "镜头标题不能为空")
        shot.update(title=item.title, duration=item.duration, preview_edited_fields=sorted(edited))
        archive_duplicate_prompts(shot, image, video)
        shot["preview_prompt_view"] = prompt_view(episode, shot, image, video)
    validate_timing(episode, video)
    validate_review_prompts(episode, image, video)
    episode["plan"]["shots"] = [
        {key: deepcopy(shot[key]) for key in ShotPlan.model_fields} for shot in episode["shots"]
    ]


def archive_duplicate_prompts(shot, *profiles):
    """Persist the repaired mapping on explicit save, retaining original AI output."""
    mapping = shot["prompts"].get("ai_parameters", {})
    for profile in profiles:
        generated = mapping.get(profile["id"], {})
        usable = usable_ai_values(profile, generated, stage_prompt=True)
        removed = {key: value for key, value in generated.items() if key not in usable}
        if removed:
            shot.setdefault("legacy_ai_prompt_parameters", {}).setdefault(profile["id"], {}).update(
                removed
            )
            if usable:
                mapping[profile["id"]] = usable
            else:
                mapping.pop(profile["id"], None)


def rebind_story(episode, image, video, reference):
    """Reconcile an unrendered story locally; callers retain the previous state in history."""
    snapshot = episode.get("preview") or {}
    if (
        "remote_video" in snapshot
        and snapshot.get("workflow_versions", {}).get(video["id"]) == video["version"]
    ):
        video = {**video, "remote_video": snapshot["remote_video"]}
    available = (episode.get("budget") or {}).get("vram_free", 0)
    budget = generation_budget(
        episode,
        video["capabilities"],
        {"devices": [{"vram_free": available}]},
        remote_video=uses_remote_video(video),
    )
    roles = role_overrides(video, parameter_overrides(episode, video))
    budget["fps"] = generation_fps(video, budget["fps"], roles.get("fps"))
    budget["render_max_duration"] = render_maximum(video, budget)
    budget["max_duration"] = min(budget["max_duration"], budget["render_max_duration"])
    ceilings = dict(budget)
    budget.update(
        {role: roles[role] for role in ("width", "height", "fps", "batch", "seed") if role in roles}
    )
    validate_strategy(
        budget, budget["max_duration"], low_memory=budget["low_memory"], ceilings=ceilings
    )
    fit_budget_dimensions(episode, video, budget)
    episode["budget"] = budget
    episode["fixed_shot_duration"] = (
        duration_seconds(video, roles["duration"], budget["fps"]) if "duration" in roles else None
    )
    for shot in episode["shots"]:
        try:
            resolve_parameters(
                video,
                {**budget, "duration": shot["duration"]},
                {},
                parameter_overrides(episode, video),
                episode.get("advanced_mode", False),
                budget,
            )
        except AppError as exc:
            raise AppError(
                "PREVIEW_INVALID",
                f"第 {shot['index'] + 1} 镜「{shot['title']}」的 {shot['duration']:g} 秒与新工作流不兼容："
                f"{exc.message}。原故事与工作流未改动，请先调整分镜或选择兼容的工作流。",
                {"shot_id": shot["id"], "field": "duration", "cause": exc.as_dict()},
            ) from exc
    # AI values belong to a workflow/stage, not to node names reused by another workflow.
    scope_ai_parameters(episode, image, video, reference)
    episode.update(
        preview_required=True, preview_approved_at=None, error=None, queued_operation=None
    )
    if (
        episode.get("bible")
        and episode["shots"]
        and all(s.get("prompts") for s in episode["shots"])
    ):
        prepare_review(episode, image, video, reference)
        episode["status"] = "AWAITING_REVIEW"
    else:
        episode["preview"] = {"workflow_versions": workflow_versions((image, video, reference))}
        episode["status"] = "DRAFT"
