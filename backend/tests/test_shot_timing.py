"""Within-shot timing is validated, editable, and reaches actual video inputs."""

import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.agents.directing import Directors
from app.agents.optimization import optimization_schema
from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts
from app.agents.timing import (
    TIMING_HEADER,
    ActionBeat,
    compile_timing,
    read_timing,
    retime_prompt,
    segment_prompt,
    timed_output,
)
from app.core.config import Settings
from app.core.errors import AppError
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_episode_workflows import imported


def beats(duration=5):
    return [
        {"start": 0, "end": 3, "action": "Reach towards the cup with a steady camera."},
        {"start": 3, "end": duration, "action": "Grasp and lift the cup, preserving framing."},
    ]


@pytest.fixture
def prompts():
    return {
        "image_prompt": "A person at a table.",
        "start_frame_prompt": "Hand beside a cup on the table.",
        "end_frame_prompt": "The same hand holds the raised cup.",
        "video_prompt": "One person, one cup, a steady close view in warm light.",
        "negative_prompt": "text, artifacts",
        "camera_motion": "static",
        "motion_strength": 0.3,
        "narration_text": "",
    }


def test_five_second_schema_compiles_timing_once_and_keeps_endpoints_clean(prompts):
    schema = timed_output(constrained_output(ShotPrompts, []), 5)
    description = schema.model_json_schema()
    assert "action_beats" in description["required"]
    assert description["properties"]["action_beats"]["minItems"] == 2
    assert description["$defs"]["ShotActionBeat"]["properties"]["end"]["maximum"] == 5
    result = compile_timing(schema.model_validate({**prompts, "action_beats": beats()}))
    assert result.video_prompt.count(TIMING_HEADER) == 1
    assert "0-3s: Reach" in result.video_prompt and "3-5s: Grasp" in result.video_prompt
    assert "action_beats" not in result.model_dump()
    assert result.start_frame_prompt == prompts["start_frame_prompt"]
    assert result.end_frame_prompt == prompts["end_frame_prompt"]
    assert result.narration_text == ""


@pytest.mark.parametrize("duration", [1, 2, 2.73, 3, 3.99])
async def test_short_shots_can_omit_timing_and_keep_one_action(prompts, duration):
    schema = timed_output(ShotPrompts, duration)
    assert compile_timing(schema.model_validate(prompts)).video_prompt == prompts["video_prompt"]
    result = await Directors(FakeProvider()).shot({}, {"duration": duration}, {})
    assert TIMING_HEADER not in result.video_prompt


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        [{"start": 0, "end": 5, "action": "Only one long phase"}],
        [{"start": 1, "end": 3, "action": "Gap at start"}, *beats()[1:]],
        [beats()[0], {"start": 2, "end": 5, "action": "Overlap"}],
        [beats()[0], {"start": 4, "end": 5, "action": "Gap"}],
        [beats()[0], {"start": 3, "end": 6, "action": "Too long"}],
        [beats()[0], {"start": 3, "end": 4, "action": "Too short"}],
        [beats()[0], {"start": 3, "end": 3, "action": "No duration"}],
        [{"start": "0", "end": 3, "action": "Coerced number"}, *beats()[1:]],
        [{"start": False, "end": 3, "action": "Boolean"}, *beats()[1:]],
        [{"start": 0, "end": float("nan"), "action": "NaN"}, *beats()[1:]],
        [{"start": 0, "end": 3.001, "action": "Too precise"}, *beats()[1:]],
        [beats()[0], {"start": 3, "end": 5, "action": "  "}],
        [beats()[0], {"start": 3, "end": 5, "action": "One\nInjected phase"}],
        [beats()[0], {"start": 3, "end": 5, "action": "Lift", "fps": 100}],
        list(reversed(beats())),
    ],
)
def test_invalid_model_timeline_is_rejected(prompts, invalid):
    with pytest.raises(ValidationError):
        timed_output(ShotPrompts, 5).model_validate({**prompts, "action_beats": invalid})


@pytest.mark.parametrize("case", ["missing", "duplicate", "overlong"])
def test_required_schedule_and_final_prompt_limit_apply_inside_provider_schema(prompts, case):
    data = {**prompts, "action_beats": beats()}
    if case == "missing":
        data.pop("action_beats")
    elif case == "duplicate":
        data["video_prompt"] += "\n" + TIMING_HEADER
    else:
        data["video_prompt"] = "x" * 5990
    with pytest.raises(ValidationError):
        timed_output(ShotPrompts, 5).model_validate(data)


@pytest.mark.parametrize("correct_on_retry", [False, True])
async def test_real_provider_serializes_schema_and_uses_existing_repair_budget(
    prompts, correct_on_retry
):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        output = {**prompts, "action_beats": beats()}
        if len(calls) == 1 or not correct_on_retry:
            output["action_beats"][1]["end"] = 6
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    provider = LLMProvider(Settings(_env_file=None, llm_model="test"), httpx.MockTransport(handle))
    operation = Directors(provider).shot({}, {"duration": 5, "action": "Lift a cup"}, {})
    if correct_on_retry:
        result = await operation
        assert "3-5s: Grasp" in result.video_prompt
    else:
        with pytest.raises(AppError) as exc:
            await operation
        assert exc.value.code == "LLM_INVALID_OUTPUT"
    assert len(calls) == 2
    instruction = calls[0]["messages"][0]["content"]
    assert "action_beats" in instruction and "0-3s" in instruction
    assert "not equal divisions" in instruction


def test_decimal_timing_and_retiming_keep_actions_and_exact_total(prompts):
    data = timed_output(ShotPrompts, 4.37).model_validate({**prompts, "action_beats": beats(4.37)})
    original = compile_timing(data).video_prompt
    resized = retime_prompt(original, 4.37, 3.63)
    _, phases = read_timing(resized, 3.63)
    assert phases[0].end == phases[1].start == 2.49
    assert phases[-1].end == 3.63
    assert [b.action for b in phases] == [b["action"] for b in beats()]
    assert retime_prompt(original, 4.37, 4.37) == original
    assert retime_prompt("Legacy visual description", 5, 3) == "Legacy visual description"


def test_oom_segments_clip_phases_and_reset_local_time(prompts):
    original = compile_timing(
        timed_output(ShotPrompts, 5).model_validate({**prompts, "action_beats": beats()})
    ).video_prompt
    first = segment_prompt(original, 5, 0, 2)
    middle = segment_prompt(original, 5, 2, 4)
    last = segment_prompt(original, 5, 4, 5)
    assert "0-2s: Reach" in first and "Grasp" not in first
    assert "0-1s: Reach" in middle and "1-2s: Grasp" in middle
    assert "0-1s: Grasp" in last and "Reach" not in last
    assert segment_prompt("Legacy action", 5, 2, 4) == "Legacy action"
    for index in range(3):
        segment = segment_prompt(original, 5, index * 5 / 3, (index + 1) * 5 / 3)
        assert read_timing(segment)[1][0].start == 0


@pytest.mark.parametrize(
    "capability,override",
    [("FIRST_LAST_TO_VIDEO", False), ("IMAGE_TO_VIDEO", False), ("IMAGE_TO_VIDEO", True)],
)
def test_timing_preview_reaches_final_workflow_but_user_override_wins(system, capability, override):
    client, app, _ = system
    profile = imported(app, capability)
    extra = {"video_workflow_id": profile["id"]}
    if override:
        extra.update(
            advanced_mode=True,
            workflow_overrides={profile["id"]: {"positive.text": "My exact prompt"}},
        )
    episode = preview(system, target_duration=5, max_shot_duration=5, **extra)
    assert len(episode["shots"]) == 1
    shot = episode["shots"][0]
    assert "0-3s:" in shot["prompts"]["video_prompt"]
    assert not app.state.store.list("job", episode["id"])
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/approve", json={"expected_version": episode["version"]}
    )
    assert response.status_code == 202, response.text
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final["error"]
    jobs = app.state.store.list("job", episode["id"])
    job = next(j for j in jobs if j["type"] == "SHOT_VIDEO")
    patched = job["patched_workflow"]["positive"]["inputs"]["text"]
    assert patched == "My exact prompt" if override else shot["prompts"]["video_prompt"] in patched
    if capability == "IMAGE_TO_VIDEO":
        assert not any(j["type"] == "SHOT_END_FRAME" for j in jobs)
    assert all(
        TIMING_HEADER not in j["patched_workflow"]["positive"]["inputs"]["text"]
        for j in jobs
        if j["type"].endswith("FRAME")
    )


def test_preview_retime_and_explicit_edit_are_atomic_and_single_source(system):
    client, app, _ = system
    episode = preview(system, target_duration=8, max_shot_duration=5)
    original = deepcopy(episode)
    body = edits(episode)
    body["shots"][0]["duration"] = 3
    body["shots"][1]["duration"] = 5
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    for s in saved["shots"]:
        assert read_timing(s["prompts"]["video_prompt"], s["duration"])
        assert s["preview_prompt_view"]["values"]["video_prompt"] == s["prompts"]["video_prompt"]
    invalid = edits(saved)
    invalid["shots"][0]["video_prompt"] = original["shots"][0]["prompts"]["video_prompt"]
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=invalid)
    assert response.status_code == 400 and response.json()["error"]["code"] == "PREVIEW_INVALID"
    assert app.state.store.get("episode", episode["id"]) == saved
    custom = edits(saved)
    custom["shots"][0]["video_prompt"] = "An intentional user-written action, no managed section."
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=custom)
    assert response.status_code == 200, response.text
    assert (
        response.json()["shots"][0]["prompts"]["video_prompt"] == custom["shots"][0]["video_prompt"]
    )
    assert not app.state.store.list("job", episode["id"])


@pytest.mark.parametrize("case", ["valid", "removed", "wrong_duration", "gap"])
def test_review_optimizer_preserves_valid_timing_or_rejects_output(prompts, case):
    prompt = compile_timing(
        timed_output(ShotPrompts, 5).model_validate({**prompts, "action_beats": beats()})
    ).video_prompt
    if case == "removed":
        prompt = "Only a loose motion description"
    elif case == "wrong_duration":
        prompt = prompt.replace("3-5s", "3-6s")
    elif case == "gap":
        prompt = prompt.replace("3-5s", "4-5s")
    data = {
        "decision": "revise",
        "confidence": "high",
        "summary": "Clarify motion",
        "limitations": "Samples only",
        "changes": {"video_prompt": {"prompt": prompt, "reason": "Clarify phases"}},
    }
    schema = optimization_schema(["video_prompt"], duration=5, timed_video=True)
    if case == "valid":
        assert schema.model_validate(data).changes["video_prompt"].prompt == prompt
    else:
        with pytest.raises(ValidationError):
            schema.model_validate(data)


def test_rescale_never_silently_discards_a_collapsed_phase():
    from app.agents.timing import compose_timing

    prompt = compose_timing(
        "Hold",
        [
            ActionBeat(start=0, end=0.01, action="settle"),
            ActionBeat(start=0.01, end=5, action="hold"),
        ],
    )
    with pytest.raises(ValueError):
        retime_prompt(prompt, 5, 1)
    assert sum(Decimal(str(b.end)) - Decimal(str(b.start)) for b in read_timing(prompt, 5)[1]) == 5
