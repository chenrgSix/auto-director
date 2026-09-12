"""Inspect without changing an episode; apply a version-bound proposal via existing rerun."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agents.directing import Directors, qa_shot_context
from app.agents.optimization import (
    optimization_schema,
    optimize_prompts,
    validate_optimized_timing,
)
from app.agents.timing import TIMING_HEADER
from app.core.errors import AppError
from app.generation.preview import PROMPT_FIELDS, prompt_view, workflow_versions
from app.generation.qa_review import VIDEO_SAMPLE_FRACTIONS, current_review_notes
from app.generation.schemas import ACTIVE, EpisodeRerun
from app.media.service import extract_frame
from app.workflows.analyzer import check_value
from app.workflows.versions import versions_match

CONTINUOUS = {"CONTINUE_FRAME", "CONTINUE_VIDEO"}


def require_idle(service, episode, expected_version):
    if episode["version"] != expected_version:
        raise AppError("CONFLICT", "短片已更新，请重新生成优化预览", status=409)
    if episode["id"] in service.busy or episode["status"] in ACTIVE:
        raise AppError("CONFLICT", "请等待或取消当前生成，再优化提示词", status=409)
    if service.engine.unresolved() or any(
        j["status"] in {"QUEUED", "RUNNING", "UNKNOWN"}
        for j in service.store.list("job", episode["id"])
    ):
        raise AppError("UNRESOLVED_JOB", "请先恢复或核对未结束的作业", status=409)
    if episode.get("preview_required") and not episode.get("preview_approved_at"):
        raise AppError("PREVIEW_REQUIRED", "请先确认分镜并生成镜头", status=409)


def find_shot(episode, shot_id):
    shot = next((s for s in episode["shots"] if s["id"] == shot_id), None)
    if not shot:
        raise AppError("NOT_FOUND", "镜头不属于当前短片", status=404)
    if not shot["enabled"] or not shot.get("prompts") or not shot.get("video_asset_id"):
        raise AppError("CONFLICT", "请选择已经生成视频的启用镜头", status=409)
    return shot


def profiles_for(service, episode):
    return [
        service.router.select(kind, episode[f"{kind}_workflow_id"]) for kind in ("image", "video")
    ]


def affected_shots(episode, selected_id):
    ids, changed = [], False
    for shot in episode["shots"]:
        if not shot["enabled"]:
            continue
        changed = shot["id"] == selected_id or (
            changed and shot["transition_from_previous"] in CONTINUOUS
        )
        if changed:
            ids.append(shot["id"])
    return ids


def validate_changes(episode, shot, profiles, changes):
    image, video = profiles
    view = prompt_view(episode, shot, image, video)
    for field, change in changes.items():
        if field not in PROMPT_FIELDS or field in view["locked"]:
            raise AppError("OPTIMIZATION_INVALID", "优化不能修改锁定或未使用的提示词")
        if change["prompt"] == shot["prompts"][field]:
            raise AppError("OPTIMIZATION_INVALID", "优化返回了未改变的提示词，请重新分析")
        if field == "video_prompt":
            try:
                validate_optimized_timing(
                    change["prompt"],
                    shot["duration"],
                    TIMING_HEADER in shot["prompts"][field],
                )
            except ValueError as exc:
                raise AppError("OPTIMIZATION_INVALID", str(exc)) from exc
        profile = video if field == "video_prompt" else image
        value = change["prompt"]
        if "negative" not in profile["bindings"] and shot["prompts"].get("negative_prompt"):
            value += (
                "\n\nVisual exclusions (do not depict these; this is not a subject list):\n"
                + shot["prompts"]["negative_prompt"].strip()
            )
        for parameter in profile["parameters"]:
            if parameter.get("role") == "prompt":
                check_value(parameter, value)
    frames = []
    if "start_frame_prompt" in changes:
        frames.append("start_frame")
    if video["capability"] == "FIRST_LAST_TO_VIDEO" and "end_frame_prompt" in changes:
        frames.append("end_frame")
    return frames


async def propose(service, episode_id, shot_id, request):
    episode = service.store.get("episode", episode_id)
    require_idle(service, episode, request.expected_version)
    shot = find_shot(episode, shot_id)
    profiles = profiles_for(service, episode)
    versions = workflow_versions(profiles)
    view = prompt_view(episode, shot, *profiles)
    editable = [field for field in PROMPT_FIELDS if field not in view["locked"]]
    enabled = [s for s in episode["shots"] if s["enabled"]]
    position = enabled.index(shot)
    previous = enabled[position - 1] if position else None
    concerns = current_review_notes(shot)
    # Strict-mode failures have no advisory notes; only use a verdict tied to this video.
    if not any(note.get("stage") == "video" for note in concerns):
        current_qa = next(
            (
                qa
                for qa in service.store.list("qa", episode_id)
                if qa["shot_id"] == shot_id
                and qa["stage"] == "video"
                and shot["video_asset_id"] in qa["asset_ids"]
            ),
            None,
        )
        if current_qa:
            concerns.append({"stage": "video", "message": current_qa["result"]["explanation"]})
    context = {
        "shot": qa_shot_context(shot),
        "effective_prompts": view["values"],
        "editable_fields": editable,
        "locked_fields": view["locked"],
        "prior_concerns": concerns,
        "user_feedback": request.feedback,
        "workflows": [
            {key: p[key] for key in ("id", "name", "capability", "capabilities")} for p in profiles
        ],
        "fixed_duration_seconds": shot["duration"],
        "frame_order": [],
    }
    # Read original assets; proposal-only samples never become persistent media records.
    with TemporaryDirectory(prefix="autodirector-optimization-") as temporary:
        paths = []
        for index, fraction in enumerate(VIDEO_SAMPLE_FRACTIONS):
            path = Path(temporary) / f"sample-{index}.png"
            await extract_frame(
                service.assets.path(shot["video_asset_id"]),
                path,
                fraction,
                duration_limit=shot["duration"],
            )
            paths.append(path)
            context["frame_order"].append(f"video_{round(fraction * 100)}_percent")
        for role in ("start_frame", "end_frame"):
            if role == "end_frame" and profiles[1]["capability"] == "IMAGE_TO_VIDEO":
                continue
            if shot.get(f"{role}_asset_id"):
                paths.append(service.assets.path(shot[f"{role}_asset_id"]))
                context["frame_order"].append(f"input_{role}")
        if previous and previous.get("actual_end_frame_asset_id"):
            paths.append(service.assets.path(previous["actual_end_frame_asset_id"]))
            context["frame_order"].append("previous_last_frame")
        result = await optimize_prompts(
            Directors(service.provider_factory()), context, paths, editable
        )
    current = service.store.get("episode", episode_id)
    require_idle(service, current, episode["version"])
    if not versions_match(versions, profiles_for(service, current)):
        raise AppError("OPTIMIZATION_STALE", "工作流已变更，请重新生成优化预览", status=409)
    data = result.model_dump()
    frames = validate_changes(episode, shot, profiles, data["changes"])
    return service.store.create(
        "prompt_optimization",
        {
            **data,
            "episode_id": episode_id,
            "shot_id": shot_id,
            "source_version": episode["version"],
            "source_video_asset_id": shot["video_asset_id"],
            "workflow_versions": versions,
            "original_prompts": {field: shot["prompts"][field] for field in PROMPT_FIELDS},
            "feedback": request.feedback,
            "locked_fields": view["locked"],
            "frames": frames,
            "affected_shot_ids": affected_shots(episode, shot_id) if data["changes"] else [],
            "scope": "keyframes" if frames else "video",
        },
        parent=episode_id,
    )


def validate_proposal(service, episode, proposal):
    if proposal["episode_id"] != episode["id"]:
        raise AppError("NOT_FOUND", "优化方案不属于当前短片", status=404)
    if proposal["source_version"] != episode["version"]:
        raise AppError("OPTIMIZATION_STALE", "方案已过期或已执行，请重新生成优化预览", status=409)
    shot = find_shot(episode, proposal["shot_id"])
    profiles = profiles_for(service, episode)
    if not versions_match(proposal["workflow_versions"], profiles):
        raise AppError("OPTIMIZATION_STALE", "工作流已变更，请重新生成优化预览", status=409)
    editable = [
        f for f in PROMPT_FIELDS if f not in prompt_view(episode, shot, *profiles)["locked"]
    ]
    result = optimization_schema(editable).model_validate(
        {k: proposal[k] for k in ("decision", "confidence", "summary", "limitations", "changes")}
    )
    if result.decision != "revise":
        raise AppError("OPTIMIZATION_INVALID", "此方案建议保留或人工处理，无需重跑")
    frames = validate_changes(episode, shot, profiles, proposal["changes"])
    if (
        frames != proposal["frames"]
        or affected_shots(episode, shot["id"]) != proposal["affected_shot_ids"]
    ):
        raise AppError("OPTIMIZATION_STALE", "重跑范围已变更，请重新生成优化预览", status=409)


def apply_proposal(service, episode_id, proposal_id, expected_version):
    episode = service.store.get("episode", episode_id)
    proposal = service.store.get("prompt_optimization", proposal_id)
    require_idle(service, episode, expected_version)
    validate_proposal(service, episode, proposal)
    return service.rerun(
        episode_id,
        EpisodeRerun(
            expected_version=expected_version,
            scope=proposal["scope"],
            shot_ids=[proposal["shot_id"]],
        ),
        optimization=deepcopy(proposal),
    )
