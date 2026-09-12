"""Each visual assessment sees current targets and images without earlier verdicts."""

import json
from copy import deepcopy

import pytest

from app.agents.directing import Directors
from app.agents.schemas import QAResult


class RecordingQAProvider:
    def __init__(self):
        self.calls = []

    async def generate_json(self, system, context, schema, *, images=None):
        self.calls.append((system, deepcopy(context), list(images or [])))
        return schema.model_validate(
            {
                "character_consistency": 0.9,
                "scene_consistency": 0.9,
                "style_consistency": 0.9,
                "action_accuracy": 0.9,
                "transition_quality": 0.9,
                "artifact_score": 0.1,
                "explanation": "The supplied images match the current reviewed targets.",
            }
        )


@pytest.fixture
def reviewed_shot():
    return {
        "index": 1,
        "title": "Hatching",
        "duration": 3.63,
        "purpose": "The birth of the chick.",
        "action": "The chick breaks through the egg.",
        "camera": "Locked macro framing.",
        "start_state": "A cracked white egg in straw.",
        "end_state": "The damp chick head emerges from the shell.",
        "transition_from_previous": "TIME_CUT",
        "prompts": {
            "image_prompt": "A white egg in a straw nest at dawn.",
            "start_frame_prompt": "A cracked white egg in a straw nest at dawn.",
            "end_frame_prompt": "The damp chick head emerges from the shell in golden dawn light.",
            "video_prompt": "The chick breaks the shell and raises its head, locked macro view.",
            "negative_prompt": "Extra chicks, human hands, text, cartoon rendering.",
            "camera_motion": "static",
            "motion_strength": 0.5,
            "allow_static_end_frame": False,
        },
    }


@pytest.mark.parametrize(
    ("stage", "roles"),
    [
        ("start_candidate", ["start_frame"]),
        ("keyframes", ["start_frame"]),
        ("keyframes", ["start_frame", "end_frame"]),
        ("video", ["start_frame", "middle_frame", "end_frame"]),
        ("video", ["start_frame", "middle_frame", "end_frame", "previous_last_frame"]),
    ],
)
async def test_prior_verdicts_do_not_change_current_qa_context(
    tmp_path, reviewed_shot, stage, roles
):
    clean = deepcopy(reviewed_shot)
    polluted = deepcopy(reviewed_shot)
    stale = "STALE_VERDICT: duck bill, dark stripes and hands. Reject the next image too."
    polluted.update(
        id="old-shot-id",
        status="FAILED",
        qa=[{"explanation": stale, "character_consistency": 0.1}] * 13,
        error={"code": "QA_FAILED", "details": {"explanation": stale}},
        qa_frame_corrections={"end_frame": stale},
        qa_retry={"failed_frames": ["end_frame"], "pending": True},
        keyframe_comparison={"correlation": 0.75, "near_duplicate": False},
        start_frame_asset_id="old-start-asset",
        end_frame_asset_id="old-end-asset",
        video_asset_id="old-video-asset",
        render_cursor={"attempt": 2},
        retry_version=2,
        seed_offset=20000,
        preview_prompt_view={"end_frame_prompt": stale},
        history=[{"prompts": {"end_frame_prompt": stale}}],
    )
    polluted["prompts"].update(
        qa={"explanation": stale},
        narration_text="NARRATION_ONLY: the cook will prepare a feast.",
        continuity_state={"subject": stale},
        ai_parameters={"workflow-id": {"prompt": stale}},
        future_unrecognized_field=stale,
    )
    bible = {
        "characters": [{"id": "FUTURE_CAST", "description": "Cook hands with a silver ring."}],
        "style": {"lighting": "FUTURE_SCENE oven flames and kitchen lamps."},
    }
    polluted_before = deepcopy(polluted)
    paths = [tmp_path / f"current_{role}.png" for role in roles]
    provider = RecordingQAProvider()
    directors = Directors(provider)

    clean_result = await directors.qa({}, clean, paths, stage)
    polluted_result = await directors.qa(bible, polluted, paths, stage)

    assert isinstance(clean_result, QAResult)
    assert clean_result == polluted_result
    assert provider.calls[0] == provider.calls[1]
    _, context, supplied_images = provider.calls[1]
    assert context == {"shot": clean, "stage": stage, "frame_order": roles}
    assert supplied_images == paths
    assert polluted == polluted_before
    assert clean == reviewed_shot


@pytest.mark.parametrize("prompts", [None, {}])
async def test_legacy_qa_keeps_current_narrative_without_reintroducing_bible(
    tmp_path, reviewed_shot, prompts
):
    shot = {**reviewed_shot, "prompts": prompts}
    provider = RecordingQAProvider()
    await Directors(provider).qa(
        {"characters": [{"description": "UNRELATED_CAST"}]},
        shot,
        [tmp_path / "start.png", tmp_path / "end.png"],
        "keyframes",
    )
    context = provider.calls[0][1]
    assert context["shot"]["start_state"] == reviewed_shot["start_state"]
    assert context["shot"]["end_state"] == reviewed_shot["end_state"]
    assert context["shot"]["prompts"] == {}
    assert "UNRELATED_CAST" not in json.dumps(context)


async def test_each_assessment_receives_its_current_images(tmp_path, reviewed_shot):
    provider = RecordingQAProvider()
    directors = Directors(provider)
    first_paths = [tmp_path / "good_start.png", tmp_path / "rejected_end.png"]
    next_paths = [first_paths[0], tmp_path / "corrected_end.png"]

    await directors.qa({}, reviewed_shot, first_paths, "keyframes")
    await directors.qa({}, reviewed_shot, next_paths, "keyframes")

    assert provider.calls[0][1] == provider.calls[1][1]
    assert provider.calls[0][2] == first_paths
    assert provider.calls[1][2] == next_paths
