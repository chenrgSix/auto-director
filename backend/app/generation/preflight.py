"""Check effective graphs without uploads, rendering or rewriting prepared content."""

from app.core.errors import AppError
from app.generation.parameters import (
    duration_seconds,
    parameter_overrides,
    resolve_parameters,
    role_overrides,
)
from app.workflows.analyzer import patch, validate_dependencies
from app.workflows.ownership import is_asset_role
from app.workflows.sequence import is_sequence


def check_graph(profile, info, values, raw, *, advanced, ai_values=None, deferred_inputs=()):
    name = profile.get("name", profile.get("id", ""))
    try:
        graph = patch(profile, values, raw, advanced=advanced, ai_values=ai_values)
        report = validate_dependencies(
            {**profile, "workflow": graph, "parameter_values": {}},
            info,
            deferred_inputs=deferred_inputs,
        )
    except AppError as exc:
        raise AppError(
            exc.code, f"工作流「{name}」生成前检查失败：{exc.message}", exc.details, exc.status
        ) from exc
    if not report["valid"]:
        descriptions = []
        for issue in report["issues"][:3]:
            field = ".".join(str(issue[k]) for k in ("node_id", "field") if issue.get(k))
            descriptions.append(f"{field} {issue['message']}".strip())
        raise AppError(
            "WORKFLOW_INVALID",
            f"工作流「{name}」生成前检查失败：{'；'.join(descriptions)}",
            report["issues"],
            status=422,
        )
    return graph


def check_profile(episode, profile, budget, info, *, duration=None):
    overrides = parameter_overrides(episode, profile)
    automatic = dict(budget)
    if duration is not None:
        automatic["duration"] = duration
    values, _, raw, _ = resolve_parameters(
        profile, automatic, {}, overrides, episode.get("advanced_mode", False), budget
    )
    # Generated prompts/assets are not known yet. Check configuration and resolved values now,
    # then check every actual value again in the engine before upload and submission.
    deferred = {
        p["key"]
        for p in profile["parameters"]
        if (p["owner"] == "ai" or is_asset_role(p.get("role")))
        and p["key"] not in overrides
        and p.get("role") not in values
    }
    check_graph(
        {
            **profile,
            "parameter_values": {
                key: value
                for key, value in profile.get("parameter_values", {}).items()
                if key not in deferred
            },
        },
        info,
        values,
        raw,
        advanced=episode.get("advanced_mode", False),
        deferred_inputs=deferred,
    )


def check_episode(episode, image, video, reference, budget, info):
    if episode.get("render_recovery"):
        return  # Settle submitted work using its immutable snapshot before new checks.
    remaining = [
        shot
        for shot in episode["shots"]
        if shot["enabled"] and not (shot["status"] == "PASSED" and shot.get("video_asset_id"))
    ]
    planning = not episode.get("plan")
    overrides = parameter_overrides(episode, video)
    roles = role_overrides(video, overrides)
    if planning:
        duration = (
            duration_seconds(video, roles["duration"], budget["fps"])
            if "duration" in roles
            else budget["max_duration"]
        )
        check_profile(episode, video, budget, info, duration=duration)
    for duration in sorted(
        {shot["duration"] for shot in remaining if not shot.get("video_asset_id")}
    ):
        check_profile(episode, video, budget, info, duration=duration)
    needs_image = planning
    previous = False
    for shot in episode["shots"]:
        if not shot["enabled"]:
            continue
        if shot in remaining:
            inherited_start = previous and shot["transition_from_previous"] in {
                "CONTINUE_FRAME",
                "CONTINUE_VIDEO",
            }
            needs_image |= (
                not shot.get("start_frame_asset_id")
                and not roles.get("start_frame")
                and not inherited_start
            ) or (
                video["capability"] == "FIRST_LAST_TO_VIDEO"
                and not shot.get("end_frame_asset_id")
                and not roles.get("end_frame")
            )
        previous = True
    if needs_image and not is_sequence(video):
        check_profile(episode, image, budget, info)
    bible = episode.get("bible")
    expected_refs = {"environment", "style"} | {
        f"character:{character['id']}" for character in (bible or {}).get("characters", [])
    }
    if not bible or expected_refs - episode.get("references", {}).keys():
        check_profile(episode, reference, budget, info)
