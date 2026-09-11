from app.core.errors import AppError


def generation_fps(profile: dict, fallback, override=None):
    fixed = profile.get("bindings", {}).get("duration", {}).get("frame_fps")
    if fixed is not None and override is not None and override != fixed:
        raise AppError(
            "WORKFLOW_INVALID",
            f"此工作流的生成帧率固定为 {fixed} FPS，不能覆盖为 {override} FPS",
        )
    return fixed if fixed is not None else override if override is not None else fallback
