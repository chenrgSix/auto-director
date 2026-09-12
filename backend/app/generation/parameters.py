"""Resolve owner values before deepcopy patch; user overrides never bypass limits."""

from copy import deepcopy

from app.core.errors import AppError
from app.core.limits import MAX_BATCH, MAX_DIMENSION, MAX_FPS, MAX_SHOT_SECONDS, MIN_DIMENSION
from app.workflows.analyzer import check_value, validate_ai_parameters
from app.workflows.dimensions import fit_dimensions
from app.workflows.duration import fit_duration, render_maximum
from app.workflows.frame_timing import frame_seconds, generation_fps
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
            "media_type": profile.get("media_type", profile.get("type")),
            "capability": profile.get("capability"),
            "source": "stage_prompt" if item.get("role") == "prompt" else "ai_parameters",
            **{
                key: item.get(key)
                for key in (
                    "key",
                    "field",
                    "role",
                    "owner",
                    "type",
                    "min",
                    "max",
                    "step",
                    "enum",
                    "downstream_constraints",
                )
                if key != "downstream_constraints" or item.get(key)
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


def usable_ai_values(profile: dict, values: dict, *, stage_prompt=False) -> dict:
    """Stage prompts have one source; a legacy AI mapping cannot replace them.

    Validate first so filtering never hides unknown keys, wrong owners or invalid types.
    Without a stage prompt, retain the legacy blank-only fallback. Custom AI parameters
    and explicit user overrides retain their existing semantics.
    """
    validate_ai_parameters(profile, values)
    prompt_keys = {p["key"] for p in profile["parameters"] if p.get("role") == "prompt"}
    return {
        key: value
        for key, value in values.items()
        if not (
            key in prompt_keys and (stage_prompt or (isinstance(value, str) and not value.strip()))
        )
    }


def include_unbound_negative(profile: dict, values: dict, overrides: dict) -> None:
    """Deliver exclusions through an instruction-only workflow's automatic prompt.

    Run after owner resolution so an explicit user prompt stays exact. The suffix check
    keeps the engine's second resolution and unsubmitted recovery idempotent. Never
    truncate either the reviewed target or exclusions to make the combined text fit.
    """
    if "negative" in profile["bindings"] or "prompt" in overrides:
        return
    binding = profile["bindings"].get("prompt")
    prompt, negative = values.get("prompt"), values.get("negative")
    if not binding or not isinstance(prompt, str) or not negative:
        return
    check_value({"key": "negative", "field": "negative", "type": "text"}, negative)
    if not negative.strip():
        return
    suffix = (
        "\n\nVisual exclusions (do not depict these; this is not a subject list):\n"
        + negative.strip()
    )
    combined = prompt if prompt.endswith(suffix) else prompt + suffix
    item = next(
        (
            item
            for item in profile["parameters"]
            if item["node_id"] == binding["node_id"] and item["field"] == binding["input"]
        ),
        None,
    )
    if item is None:
        return  # Let the binding validator report an actionable configuration error.
    try:
        check_value(item, combined)
    except AppError as exc:
        raise AppError(
            exc.code,
            f"提示词合并负向限制后不满足工作流参数约束：{exc.message}",
            exc.details,
        ) from exc
    values["prompt"] = combined


def duration_seconds(profile: dict, value, fps: float) -> float:
    binding = profile["bindings"].get("duration", {})
    if binding.get("transform") == "duration_to_frames":
        value = frame_seconds(binding, value, fps)
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
        raise AppError(
            "WORKFLOW_INVALID",
            f"单镜时长 {values['duration']} 秒超出配置上限（允许 1～{maximum:g} 秒）",
        )


def fit_budget_dimensions(episode, video, budget):
    """Keep explicit episode dimensions exact; adapt legacy automatic budgets in place."""
    explicit = [role for role in ("width", "height") if episode.get(role) is not None]
    if explicit:
        budget["explicit_dimensions"] = explicit
    else:
        budget.pop("explicit_dimensions", None)
    locked = set(explicit) | role_overrides(video, parameter_overrides(episode, video)).keys()
    budget.update(fit_dimensions(video, budget, locked=locked))


def resolve_parameters(
    profile, automatic, assets, overrides, advanced, budget=None, *, recovery=False, ai_values=None
):
    validate_ai_parameters(profile, ai_values or {})
    validate_overrides(profile, overrides, advanced)
    values, bound_assets, raw = dict(automatic), dict(assets), dict(overrides)
    values.update(role_overrides(profile, ai_values or {}))
    roles = role_overrides(profile, overrides)
    if profile.get("bindings", {}).get("duration", {}).get("frame_fps"):
        values["fps"] = generation_fps(profile, values.get("fps", 16), roles.get("fps"))
    for role, value in roles.items():
        if is_asset_role(role):
            if not (recovery and role in {"start_frame", "end_frame"} and assets.get(role)):
                bound_assets[role] = value
        elif role != "duration":
            values[role] = value
    include_unbound_negative(profile, values, roles)
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
    values = fit_dimensions(
        profile,
        values,
        automatic=budget is not None,
        locked=()
        if recovery
        else set(roles)
        | set(role_overrides(profile, ai_values or {}))
        | set((budget or {}).get("explicit_dimensions", [])),
        ceilings=budget if (budget or {}).get("low_memory") else None,
    )
    validate_strategy(
        values, maximum, low_memory=(budget or {}).get("low_memory", False), ceilings=budget
    )
    if profile.get("media_type", profile.get("type")) == "video" and "duration" in values:
        timeline = values["duration"]
        fps = values.get("fps", 16)
        maximum = render_maximum(profile, {**(budget or {}), "fps": fps})
        values["duration"] = fit_duration(profile, timeline, maximum, fps=fps)
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
