"""Resolve owner values before deepcopy patch; user overrides never bypass limits."""

from copy import deepcopy

from app.core.errors import AppError
from app.core.limits import MAX_BATCH, MAX_DIMENSION, MAX_FPS, MAX_SHOT_SECONDS, MIN_DIMENSION
from app.workflows.analyzer import check_value, validate_ai_parameters
from app.workflows.duration import fit_duration, render_maximum
from app.workflows.ownership import decorate_parameters, is_asset_role


def parameter_overrides(episode: dict, profile: dict) -> dict:
    if not episode.get("advanced_mode", False):
        return {}
    media = profile.get("media_type", profile.get("type"))
    legacy = (
        episode.get(f"{media}_parameters", {})
        if profile["id"] == episode.get(f"{media}_workflow_id")
        else {}
    )
    return {**legacy, **episode.get("workflow_overrides", {}).get(profile["id"], {})}


def ai_parameters(*profiles: dict) -> list[dict]:
    return [
        {
            "workflow_id": profile["id"],
            **{
                key: item.get(key)
                for key in ("key", "field", "role", "owner", "type", "min", "max", "enum")
            },
        }
        for profile in profiles
        for item in profile["parameters"]
        if item["owner"] == "ai"
    ]


def validate_overrides(profile: dict, overrides: dict, advanced: bool) -> None:
    if overrides and not advanced:
        raise AppError("OVERRIDE_NOT_ALLOWED", "参数覆盖需要高级模式")
    decorated = deepcopy(profile)
    decorate_parameters(decorated)
    parameters = {p["key"]: p for p in decorated["parameters"]}
    for key, value in overrides.items():
        item = parameters.get(key)
        if not item:
            raise AppError("WORKFLOW_INVALID", f"未知动态参数 {key}")
        if not item["editable"] or item["override_policy"] != "advanced":
            raise AppError("OVERRIDE_NOT_ALLOWED", f"参数 {key} 不允许覆盖")
        check_value(item, value)


def role_overrides(profile: dict, overrides: dict) -> dict:
    parameters = {p["key"]: p for p in profile["parameters"]}
    return {
        parameters[key]["role"]: value
        for key, value in overrides.items()
        if key in parameters and parameters[key].get("role")
    }


def duration_seconds(profile: dict, value, fps: float) -> float:
    binding = profile["bindings"].get("duration", {})
    if binding.get("transform") == "duration_to_frames":
        value = (value - binding.get("frame_offset", 1)) / fps
    return float(value)


def validate_strategy(values: dict, maximum: float, *, low_memory=False, ceilings=None) -> None:
    for role, minimum, upper in [
        ("width", MIN_DIMENSION, MAX_DIMENSION),
        ("height", MIN_DIMENSION, MAX_DIMENSION),
        ("fps", 1, MAX_FPS),
        ("batch", 1, MAX_BATCH),
    ]:
        value = values.get(role)
        if value is None:
            continue
        if (
            type(value) is not int
            or not minimum <= value <= upper
            or (role in {"width", "height"} and value % 16)
        ):
            raise AppError("WORKFLOW_INVALID", f"{role} 不满足系统范围或对齐约束")
        if (
            low_memory
            and ceilings
            and role in {"width", "height", "batch"}
            and value > ceilings[role]
        ):
            raise AppError("WORKFLOW_INVALID", f"{role} 超出低显存策略上限")
    if "duration" in values and (
        type(values["duration"]) not in {int, float}
        or not 1 <= values["duration"] <= maximum + 1e-9
    ):
        raise AppError("WORKFLOW_INVALID", "单镜时长超出 workflow / 显存策略上限")


def resolve_parameters(
    profile, automatic, assets, overrides, advanced, budget=None, *, recovery=False, ai_values=None
):
    validate_ai_parameters(profile, ai_values or {})
    validate_overrides(profile, overrides, advanced)
    values, bound_assets, raw = dict(automatic), dict(assets), dict(overrides)
    values.update(role_overrides(profile, ai_values or {}))
    roles = role_overrides(profile, overrides)
    for role, value in roles.items():
        if is_asset_role(role):
            if not (recovery and role in {"start_frame", "end_frame"} and assets.get(role)):
                bound_assets[role] = value
        elif role != "duration":
            values[role] = value
    if "duration" in roles and not recovery:
        duration = duration_seconds(profile, roles["duration"], values.get("fps", 16))
        if "duration" in automatic and abs(duration - automatic["duration"]) > 0.011:
            raise AppError("OVERRIDE_INVALID", "时长覆盖必须先进入导演计划，不能改变已计划时间线")
        values["duration"] = duration
    if recovery:
        for role in ("width", "height", "batch", "duration"):
            if role in automatic:
                values[role] = automatic[role]
    for item in profile["parameters"]:
        if item.get("role"):
            raw.pop(
                item["key"], None
            )  # Semantic overrides now live in values/assets, preserving transforms.
    maximum = min(
        profile["capabilities"]["max_duration"],
        (budget or {}).get("max_duration", MAX_SHOT_SECONDS),
    )
    validate_strategy(
        values, maximum, low_memory=(budget or {}).get("low_memory", False), ceilings=budget
    )
    if profile.get("media_type", profile.get("type")) == "video" and "duration" in values:
        timeline = values["duration"]
        values["duration"] = fit_duration(profile, timeline, render_maximum(profile, budget))
        values["timeline_duration"] = timeline
    sources = {}
    for item in profile["parameters"]:
        role = item.get("role")
        sources[item["key"]] = (
            "user"
            if item["key"] in overrides
            else "ai"
            if item["key"] in (ai_values or {})
            else item.get("owner", "workflow")
            if role in values or role in bound_assets
            else "workflow"
        )
        if recovery and role in {
            "width",
            "height",
            "batch",
            "duration",
            "start_frame",
            "end_frame",
        }:
            sources[item["key"]] = "system:oom"
    return values, bound_assets, raw, sources
