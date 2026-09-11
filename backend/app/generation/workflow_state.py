"""Non-destructive workflow replacement and recovery of legacy reset snapshots."""

from copy import deepcopy

from app.generation.parameters import usable_ai_values

STORY_FIELDS = (
    "title",
    "status",
    "plan",
    "bible",
    "shots",
    "references",
    "continuity",
    "budget",
    "fixed_shot_duration",
    "final_video_asset_id",
    "final_duration",
    "error",
    "warnings",
    "started_at",
    "completed_at",
    "queued_operation",
    "preview",
    "preview_required",
    "preview_approved_at",
    "refresh_workflow_budget",
)
SHOT_ASSETS = (
    "start_frame_asset_id",
    "end_frame_asset_id",
    "actual_end_frame_asset_id",
    "video_asset_id",
)


def media_ids(episode):
    ids = set(episode.get("references", {}).values())
    ids.update(
        shot[field] for shot in episode.get("shots", []) for field in SHOT_ASSETS if shot.get(field)
    )
    if episode.get("final_video_asset_id"):
        ids.add(episode["final_video_asset_id"])
    return ids


def recoverable_history(episode):
    if episode.get("plan") or episode.get("shots") or episode.get("bible") or media_ids(episode):
        return None
    return next(
        (
            item
            for item in reversed(episode.get("workflow_binding_history", []))
            if item["previous_state"].get("plan") and item["previous_state"].get("shots")
        ),
        None,
    )


def scope_ai_parameters(episode, image, video, reference):
    outputs = [(episode.get("bible"), (reference,))]
    outputs += [(shot.get("prompts"), (image, video)) for shot in episode["shots"]]
    for output, profiles in outputs:
        if output is None:
            continue
        mapping = output.get("ai_parameters", {})
        selected = {p["id"] for p in profiles}
        for id in list(mapping):
            if id not in selected:
                mapping.pop(id)
        for profile in profiles:
            usable_ai_values(profile, mapping.get(profile["id"], {}), stage_prompt=True)


def resume_story(episode, image, video, reference):
    """Keep produced media authoritative; new workflows only produce missing outputs."""
    scope_ai_parameters(episode, image, video, reference)
    episode.update(
        status="COMPLETED" if episode["status"] == "COMPLETED" else "DRAFT",
        error=None,
        queued_operation=None,
        refresh_workflow_budget=True,
        preview={
            "workflow_versions": {p["id"]: p["version"] for p in (image, video, reference)},
            "capability": video["capability"],
        },
    )
    # Keep the existing approval and budget (also needed for re-export). Refresh the
    # latter from live device/node information only when the user resumes rendering.


def story_snapshot(episode):
    return {key: deepcopy(episode[key]) for key in STORY_FIELDS if key in episode}
