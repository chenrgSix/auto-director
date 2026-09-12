"""Persist rejected outputs separately from reviewed prompts and submitted job recovery."""

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


def corrected_prompt(prompt: str, shot: dict, role: str) -> str:
    correction = (shot.get("qa_frame_corrections") or {}).get(role)
    if not correction:
        return prompt
    return (
        prompt
        + "\n\nCorrection from visual QA for this frame only (subordinate to the requested target):\n"
        + correction
        + "\nCorrect these defects while preserving the requested subject, lifecycle stage, "
        "setting and framing. Do not introduce subjects or props from other shots."
    )
