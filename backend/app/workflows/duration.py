"""Adapt timeline seconds to legal render seconds; frame-count bindings retain their transform."""

import math
from fractions import Fraction

from app.core.errors import AppError
from app.core.limits import MAX_SHOT_SECONDS
from app.workflows.analyzer import check_value


def duration_limits(episode: dict, capabilities: dict) -> dict:
    """Use configured limits; device free memory cannot predict a model's clip capacity."""
    maximum = min(capabilities["max_duration"], MAX_SHOT_SECONDS)
    return {
        "max_duration": min(maximum, episode.get("max_shot_duration") or MAX_SHOT_SECONDS),
        "render_max_duration": maximum,
    }


def fit_duration(profile: dict, seconds: float, maximum: float, *, round_down=False) -> float:
    binding = profile.get("bindings", {}).get("duration", {})
    if binding.get("transform", "identity") != "identity":
        return seconds
    key = f"{binding.get('node_id')}.{binding.get('input')}"
    item = next((p for p in profile["parameters"] if p["key"] == key), None)
    if not item:
        return seconds
    lower = max(1, item.get("min") or 1)
    upper = min(maximum, item["max"] if item.get("max") is not None else maximum)
    if item.get("enum"):
        candidates = [v for v in item["enum"] if type(v) in {int, float} and math.isfinite(v)]
    elif item["type"] == "integer":
        candidates = list(range(math.ceil(lower), math.floor(upper) + 1))
    elif item["type"] == "number":
        value = min(seconds, upper) if round_down else max(seconds, lower)
        step = item.get("step")
        if step and step > 0:
            origin, stride = Fraction(str(item.get("min") or 0)), Fraction(str(step))
            units = (Fraction(str(value)) - origin) / stride
            value = float(origin + (math.floor(units) if round_down else math.ceil(units)) * stride)
        candidates = [value]
    else:
        candidates = []
    valid = []
    for value in candidates:
        if not lower <= value <= upper:
            continue
        if (round_down and value > seconds) or (not round_down and value < seconds):
            continue
        try:
            check_value(item, value)
        except AppError:
            continue
        valid.append(value)
    if not valid:
        raise AppError(
            "WORKFLOW_INVALID",
            f"视频时长无可用值：参数 {key} 要求 {lower:g}～{upper:g} 秒内的合法值，"
            f"当前目标 {seconds:g} 秒；请检查模型时长与工作流/显存上限",
            {"parameter": key, "requested_seconds": seconds, "min": lower, "max": upper},
        )
    return max(valid) if round_down else min(valid)


def uses_remote_video(profile: dict) -> bool:
    """Prefer live metadata; old persisted profiles retain verified API node identities."""
    if "remote_video" in profile:
        return profile["remote_video"] is True
    node_id = profile.get("bindings", {}).get("duration", {}).get("node_id")
    node = profile.get("workflow", {}).get(node_id, {})
    return profile.get("media_type", profile.get("type")) == "video" and any(
        item.get("id") == node_id and item.get("class_type") == node.get("class_type")
        for item in profile.get("execution_info", {}).get("api_nodes", [])
    )


def render_maximum(profile: dict, budget: dict | None = None) -> float:
    budget = budget or {}
    maximum = min(
        profile["capabilities"]["max_duration"],
        budget.get("render_max_duration", budget.get("max_duration", MAX_SHOT_SECONDS)),
    )
    return fit_duration(profile, maximum, maximum, round_down=True)
