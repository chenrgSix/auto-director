"""Fit automatic render dimensions to real workflow fields without increasing a budget."""

from app.core.errors import AppError
from app.core.limits import MAX_DIMENSION, MIN_DIMENSION
from app.workflows.analyzer import check_value


def fit_dimensions(profile, values, *, automatic=True, locked=(), ceilings=None):
    result = dict(values)
    parameters = {p.get("role"): p for p in profile["parameters"] if p.get("role")}
    for role in ("width", "height"):
        item = parameters.get(role)
        value = result.get(role)
        if item is None or role not in profile["bindings"] or value is None:
            continue
        if not automatic or role in locked or item.get("owner") != "system":
            check_value(item, value)
            continue
        if type(value) is not int or not MIN_DIMENSION <= value <= MAX_DIMENSION:
            raise AppError("WORKFLOW_INVALID", f"自动 {role}={value} 超出系统尺寸范围")
        upper = min(value, (ceilings or {}).get(role, value))
        for candidate in range(upper // 16 * 16, MIN_DIMENSION - 1, -16):
            try:
                check_value(item, candidate)
            except AppError:
                continue
            result[role] = candidate
            break
        else:
            raise AppError(
                "WORKFLOW_INVALID",
                f"工作流「{profile.get('name', profile.get('id', ''))}」的 {role} "
                f"在 {MIN_DIMENSION}～{upper} 像素预算内没有合法尺寸，请调整工作流或尺寸配置",
                {"role": role, "requested": value, "maximum": upper, "parameter": item},
            )
    return result
