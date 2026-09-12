"""Advisory review identity and warnings, separate from render success."""

import hashlib
import json

from app.agents.directing import qa_shot_context


def video_review_key(episode: dict, shot: dict, previous: dict | None, settings) -> str:
    context = {
        "contract": 1,
        "video": shot["video_asset_id"],
        "previous_end": (previous or {}).get("actual_end_frame_asset_id"),
        "target": qa_shot_context(shot),
        "quality": episode["quality"],
        "policy": episode.get("qa_policy", "strict"),
        "model": settings.vlm_model,
        "endpoint": settings.llm_base_url,
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def current_review_notes(shot: dict) -> list[dict]:
    assets = {
        shot.get("start_frame_asset_id"),
        shot.get("end_frame_asset_id"),
        shot.get("video_asset_id"),
    }
    return [
        note
        for note in shot.get("review_notes", [])
        if set(note.get("asset_ids", [])).issubset(assets)
    ]
