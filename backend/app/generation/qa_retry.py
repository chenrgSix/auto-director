"""Persist rejected outputs separately from reviewed prompts and submitted job recovery."""

from app.workflows.schema import WorkflowCapability

FRAME_ROLES = ("start_frame", "end_frame")


def failed_frames(scope: str, needs_end: bool) -> list[str]:
    if scope == "video":
        return []
    if scope == "transition":
        return ["end_frame" if needs_end else "start_frame"]
    return list(FRAME_ROLES if needs_end else FRAME_ROLES[:1])


def retry_state(shot: dict, scope: str, frames: list[str]) -> dict:
    return {
        "pending": True,
        "scope": scope,
        "failed_frames": frames,
        "rejected_assets": {
            f"{role}_asset_id": shot.get(f"{role}_asset_id")
            for role in [*frames, "video", "actual_end_frame"]
        },
    }


def consume_retry(shot: dict) -> dict:
    state = shot.get("qa_retry") or {}
    if not state.get("pending"):
        return {}
    # A manually replaced binding is not the rejected output anymore.
    return {
        **{
            field: None
            for field, asset_id in state["rejected_assets"].items()
            if asset_id and shot.get(field) == asset_id
        },
        "qa_retry": {**state, "pending": False},
    }


def correction_reference(profile: dict, shot: dict, role: str, overrides: dict | None = None):
    if (
        profile.get("capability") != WorkflowCapability.IMAGE_TO_IMAGE
        or "reference_image" not in profile.get("bindings", {})
        or not (shot.get("qa_frame_corrections") or {}).get(role)
    ):
        return None
    rejected = (shot.get("qa_retry") or {}).get("rejected_assets", {}).get(f"{role}_asset_id")
    override = (overrides or {}).get("reference_image")
    return rejected if not override or override == rejected else None


def corrected_prompt(prompt: str, shot: dict, role: str, *, editing_rejected=False) -> str:
    correction = (shot.get("qa_frame_corrections") or {}).get(role)
    if not correction:
        return prompt
    return (
        prompt
        + (
            "\n\nThe supplied reference image is the rejected frame to repair. "
            "Apply a targeted edit to this image to meet the requested target; "
            "preserve the correct composition, background and other unaffected content."
            if editing_rejected
            else ""
        )
        + "\n\nCorrection from visual QA for this frame only (subordinate to the requested target):\n"
        + correction
        + "\nCorrect these defects while preserving the requested subject, lifecycle stage, "
        "setting and framing. Do not introduce subjects or props from other shots."
    )
