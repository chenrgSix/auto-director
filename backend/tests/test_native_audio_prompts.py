"""Speech survives Agent preparation, edits and patch without becoming an image prompt."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.audio import H3, audio_section, uses_h3_audio, validate_h3_prompt
from app.agents.directing import Directors
from app.agents.optimization import optimization_schema
from app.agents.schemas import ShotPrompts
from app.agents.shot_batch import shot_output
from app.agents.timing import retime_prompt
from app.core.errors import AppError
from app.generation.parameters import ai_parameters, resolve_parameters
from app.generation.pipeline import GenerationService
from app.workflows.frame_timing import frame_count
from app.workflows.manager import WorkflowManager
from app.workflows.schema import WorkflowPatch
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview
from tests.test_prompt_batches import shots

LINE = "故事才刚刚开始。"
PROMPT = (
    "integrated_multimodal_description: [Shot 1] One adult woman looks up in a quiet room.\n\n"
    "Speech performance: The narrator with a clear warm female Mandarin voice (S1) says in "
    f"an off-screen voiceover: <d>[Chinese] {LINE}</d> once. The visible woman keeps her lips closed.\n\n"
    "overall_soundscape: Soft indoor room tone, clear voice, no other voices.\n\n"
    "non_diegetic_music: None."
)


def specifications():
    return [
        {
            "workflow_id": "h3",
            "media_type": "video",
            "role": "prompt",
            "owner": "ai",
            "key": "5.prompt",
            "type": "text",
            "audio_prompt_format": H3,
        }
    ]


class AudioProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, ShotPrompts) and uses_h3_audio(
            context.get("workflow_parameters", [])
        ):
            assert "off-screen voiceover" in system
            # Make a complete response, then use the real scoped schema to validate it.
            base = await super().generate_json(system, context, ShotPrompts, images=images)
            data = base.model_dump()
            data.update(video_prompt=PROMPT, narration_text=LINE)
            duration = context["shot"]["duration"]
            if duration >= 4:
                data["action_beats"] = [
                    {"start": 0, "end": 2, "action": "Look up, lips closed."},
                    {"start": 2, "end": duration, "action": "Hold gaze, lips closed."},
                ]
            return schema.model_validate(data)
        return await super().generate_json(system, context, schema, images=images)


@pytest.mark.parametrize("batch", [False, True])
async def test_single_and_batch_share_audio_timing_and_image_separation(batch):
    agents = Directors(AudioProvider())
    if batch:
        results = await agents.shot_batch({}, shots([5, 2]), {}, specifications(), idea="中文旁白")
    else:
        results = [await agents.shot({}, {"duration": 5}, {}, specifications(), idea="中文旁白")]
    for result in results:
        assert LINE in result.video_prompt and result.narration_text == LINE
        assert result.ai_parameters == {}
        for field in ("image_prompt", "start_frame_prompt", "end_frame_prompt"):
            assert LINE not in getattr(result, field)
    original = results[0].video_prompt
    changed = retime_prompt(original, 5, 4)
    assert audio_section(changed) == audio_section(original)
    assert "2.0" not in changed  # No second persistent speech timing source.


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "order",
        "empty",
        "bad_tag",
        "missing_words",
        "image_speech",
        "shot_number",
        "voiceover",
    ],
)
async def test_scoped_schema_rejects_invalid_or_undelivered_speech(kind):
    data = (
        await FakeProvider().generate_json("", {"shot": {"duration": 2}}, ShotPrompts)
    ).model_dump()
    data.update(video_prompt=PROMPT, narration_text=LINE)
    if kind == "missing":
        data["video_prompt"] = "Woman looking up"
    elif kind == "order":
        data["video_prompt"] = PROMPT.replace("Speech performance:", "non_diegetic_music:")
    elif kind == "empty":
        data["video_prompt"] = PROMPT.replace("None.", "")
    elif kind == "bad_tag":
        data["video_prompt"] = PROMPT.replace("</d>", "")
    elif kind == "missing_words":
        data["narration_text"] = "另一个没有送去视频生成的旁白"
    elif kind == "shot_number":
        data["video_prompt"] = PROMPT.replace("[Shot 1]", "[Shot 2]")
    elif kind == "voiceover":
        data["video_prompt"] = PROMPT.replace("says in an off-screen voiceover", "says")
    else:
        data["start_frame_prompt"] = f"Woman says <d>[Chinese] {LINE}</d>"
    with pytest.raises(ValidationError):
        shot_output(specifications(), 2).model_validate(data)


async def test_custom_provider_cannot_bypass_audio_checks():
    class BypassProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            return await super().generate_json(system, context, ShotPrompts)

    with pytest.raises(AppError, match="分镜动作时间线"):
        await Directors(BypassProvider()).shot({}, {"duration": 2}, {}, specifications())


async def test_unconfigured_workflows_keep_legacy_narration_behavior():
    old = await Directors(FakeProvider()).shot({}, {"duration": 2}, {})
    assert old.video_prompt == "Lion walking" and "Speech performance:" not in old.video_prompt
    assert validate_h3_prompt(PROMPT.replace(f"<d>[Chinese] {LINE}</d>", "No speech."))


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("batch_size", [1, 3])
def test_preview_saved_speech_reaches_final_json_and_user_override_wins(
    system, override, batch_size
):
    client, app, comfy = system
    profile = app.state.store.get("workflow", "default_video")
    response = client.patch(
        "/api/v1/workflows/default_video",
        json={
            "capability": profile["capability"],
            "capabilities": {**profile["capabilities"], "audio_prompt_format": H3},
        },
    )
    assert response.status_code == 200, response.text
    app.state.generation.provider_factory = AudioProvider
    app.state.generation.settings.prompt_batch_size = batch_size
    extra = (
        {
            "advanced_mode": True,
            "workflow_overrides": {"default_video": {"positive.text": "User silent scene"}},
        }
        if override
        else {}
    )
    episode = preview(system, target_duration=6, **extra)
    assert not comfy.prompts
    original = deepcopy(episode["shots"])
    changes = edits(episode)
    if not override:
        for shot in changes["shots"]:
            shot["video_prompt"] = shot["video_prompt"].replace(LINE, "我们出发吧。")
    response = client.patch(f"/api/v1/episodes/{episode['id']}/preview", json=changes)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/approve", json={"expected_version": saved["version"]}
        ).status_code
        == 202
    )
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final.get("error")
    jobs = app.state.store.list("job", episode["id"])
    for job in jobs:
        prompt = job["patched_workflow"]["positive"]["inputs"]["text"]
        if job["type"] == "SHOT_VIDEO":
            if override:
                assert prompt == "User silent scene"
            else:
                assert "我们出发吧。" in prompt and LINE not in prompt
                assert prompt.count("How the reference pictures align") == 1
        else:
            assert LINE not in prompt and "我们出发吧。" not in prompt
    assert (
        original[0]["prompts"]["narration_text"] == LINE
    )  # Display copy never overrides edited video.


def test_header_uses_actual_frame_duration_and_resolution_is_idempotent(system):
    _, app, _ = system
    profile = app.state.store.get("workflow", "default_video")
    profile["capabilities"]["audio_prompt_format"] = H3
    specs = ai_parameters(profile)
    assert uses_h3_audio(specs)
    automatic = {"prompt": PROMPT, "duration": 5, "fps": 16, "width": 256, "height": 256}
    once = resolve_parameters(profile, automatic, {}, {}, False)[0]
    twice = resolve_parameters(profile, once, {}, {}, False)[0]
    assert once["prompt"] == twice["prompt"]
    actual_duration = (
        frame_count(profile["bindings"]["duration"], once["duration"], once["fps"]) / once["fps"]
    )
    assert f"{actual_duration:.2f}-second mark" in once["prompt"]
    assert "Picture 2" in once["prompt"]
    profile["capability"] = "IMAGE_TO_VIDEO"
    i2v = resolve_parameters(profile, automatic, {}, {}, False)[0]
    assert "<Picture 1>" in i2v["prompt"] and "<Picture 2>" not in i2v["prompt"]


def test_image_workflow_cannot_claim_video_audio(system):
    _, app, _ = system
    with pytest.raises(AppError, match="仅适用于视频"):
        WorkflowManager(app.state.store).update(
            "default_image", WorkflowPatch(capabilities={"audio_prompt_format": H3})
        )


@pytest.mark.parametrize("mutation", ["words", "voice", "remove", "visual"])
def test_visual_optimization_preserves_entire_audio_direction(mutation):
    prompt = PROMPT
    if mutation == "words":
        prompt = prompt.replace(LINE, "Wrong line")
    elif mutation == "voice":
        prompt = prompt.replace("female Mandarin", "male English")
    elif mutation == "remove":
        prompt = "Woman looks up."
    else:
        prompt = prompt.replace("looks up", "slowly raises her gaze")
    schema = optimization_schema(["video_prompt"], original_video=PROMPT)
    data = {
        "decision": "revise",
        "confidence": "high",
        "summary": "fix",
        "limitations": "sampled",
        "changes": {"video_prompt": {"prompt": prompt, "reason": "visible fix"}},
    }
    if mutation == "visual":
        assert schema.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            schema.model_validate(data)


@pytest.mark.parametrize("fallback", [False, True])
async def test_speech_oom_does_not_repeat_lines_in_temporal_segments(fallback):
    calls = []

    async def render(*args):
        calls.append(args)
        if len(calls) <= 2:
            raise AppError("OUT_OF_MEMORY", "fixture")
        return {"id": "fallback-video"}

    alternative = {
        "id": "small",
        "capability": "IMAGE_TO_VIDEO",
        "capabilities": {"audio_prompt_format": H3, "max_duration": 5},
    }
    service = SimpleNamespace(
        render=render,
        warn=lambda *args: None,
        router=SimpleNamespace(select=lambda *args: alternative),
    )
    episode = {"id": "ep", "bible": {}}
    shot = {
        "id": "s",
        "duration": 5,
        "start_frame_asset_id": "start",
        "end_frame_asset_id": None,
        "prompts": {"video_prompt": PROMPT, "continuity_state": {}},
    }
    profile = {
        "id": "h3",
        "capability": "IMAGE_TO_VIDEO",
        "parameters": [],
        "capabilities": {
            "audio_prompt_format": H3,
            "max_duration": 5,
            "low_memory_workflow_id": "small" if fallback else None,
        },
    }
    base = {"width": 640, "height": 384, "seed": 1}
    task = GenerationService.render_video(
        service, episode, shot, {}, profile, base, {}, {}, "step", {"remaining": 3}
    )
    if fallback:
        assert (await task)["id"] == "fallback-video"
        assert len(calls) == 3 and calls[-1][3]["duration"] == 5
    else:
        with pytest.raises(AppError, match="避免分段重复台词"):
            await task
        assert len(calls) == 2
    assert all(call[2] == "SHOT_VIDEO" for call in calls)
