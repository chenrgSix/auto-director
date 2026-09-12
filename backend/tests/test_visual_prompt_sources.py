import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from app.agents.directing import Directors, anchored_prompt
from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts, VisualBible
from app.core.config import Settings
from app.core.errors import AppError
from app.generation.parameters import ai_parameters, usable_ai_values
from tests.fakes import FakeProvider
from tests.test_ai_parameters import CreativeProvider, configure_ai_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_preview import edits, preview


@pytest.mark.parametrize("base", [ShotPrompts, VisualBible])
async def test_stage_prompt_is_not_a_second_agent_output(system, base):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    specifications = ai_parameters(app.state.store.get("workflow", "default_image"))
    prompt = next(item for item in specifications if item["role"] == "prompt")
    assert prompt["source"] == "stage_prompt"
    assert prompt["media_type"] == "image" and prompt["capability"] == "TEXT_TO_IMAGE"
    schema = constrained_output(base, specifications)
    fields = schema.model_json_schema()["properties"]["ai_parameters"]["properties"]
    assert "positive.text" not in fields["default_image"]["properties"]
    assert "sampler.denoise" in fields["default_image"]["properties"]
    result = await FakeProvider().generate_json("", {}, schema)
    with pytest.raises(ValidationError):
        schema.model_validate(
            {**result.model_dump(), "ai_parameters": {"default_image": {"positive.text": "旁白"}}}
        )


async def test_duplicate_prompt_uses_provider_correction_and_keeps_narration_separate(system):
    _, app, _ = system
    specifications = ai_parameters(app.state.store.get("workflow", "default_image"))
    schema = constrained_output(ShotPrompts, specifications)
    result = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    result["narration_text"] = "这三只老虎走进了侏罗纪。"
    invalid = {**result, "ai_parameters": {"default_image": {"positive.text": "这是一段旁白"}}}
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        output = invalid if len(calls) == 1 else result
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handle)
    )
    output = await provider.generate_json("", {}, schema)
    assert len(calls) == 2 and output.ai_parameters == {}
    assert output.narration_text == result["narration_text"]
    assert output.start_frame_prompt != output.end_frame_prompt


@pytest.mark.parametrize(
    "field", ["image_prompt", "start_frame_prompt", "end_frame_prompt", "video_prompt"]
)
async def test_silent_narration_does_not_allow_blank_visual_prompt(field):
    output = await FakeProvider().generate_json("", {}, ShotPrompts)
    assert output.narration_text == ""
    with pytest.raises(ValidationError):
        ShotPrompts.model_validate({**output.model_dump(), field: " \n\t"})


async def test_shot_agent_receives_idea_and_explicit_visual_narration_contract():
    class InspectProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            assert context["idea"] == "三只老虎穿越，要有旁白"
            assert "narration_text" in system and "source=stage_prompt" in system
            assert "silent shot" in system and "never spoken narration" in system
            return await super().generate_json(system, context, schema, images=images)

    await Directors(InspectProvider()).shot({}, {"duration": 3}, {}, idea="三只老虎穿越，要有旁白")


@pytest.mark.parametrize("legacy", ["", "只有旁白，没有画面描述"])
@pytest.mark.parametrize("override", [False, True])
def test_each_render_uses_its_stage_prompt_and_retains_custom_ai_and_user_priority(
    system, legacy, override
):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)

    class LegacyNarrationProvider(CreativeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            output = await super().generate_json(system, context, schema, images=images)
            if issubclass(schema, (ShotPrompts, VisualBible)):
                data = output.model_dump()
                for item in context["workflow_parameters"]:
                    if item["role"] == "prompt":
                        data["ai_parameters"].setdefault(item["workflow_id"], {})[item["key"]] = (
                            legacy
                        )
                if issubclass(schema, ShotPrompts):
                    data.pop(
                        "action_beats", None
                    )  # Simulate legacy output without timing metadata.
                    data["narration_text"] = "旁白脚本独立存储，不进入画面输入"
                    return ShotPrompts.model_validate(data)
                return VisualBible.model_validate(data)
            return output

    app.state.generation.provider_factory = LegacyNarrationProvider
    extra = (
        {
            "advanced_mode": True,
            "workflow_overrides": {
                "default_image": {"positive.text": "User image"},
                "default_video": {"positive.text": "User video"},
            },
        }
        if override
        else {}
    )
    episode = preview(system, target_duration=1, **extra)
    shot = episode["shots"][0]
    for field, value in shot["preview_prompt_view"]["values"].items():
        expected = (
            ("User video" if field == "video_prompt" else "User image")
            if override
            else shot["prompts"][field]
        )
        assert value == expected
    assert not comfy.prompts
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/approve",
            json={"expected_version": episode["version"]},
        ).status_code
        == 202
    )
    final = wait_episode(client, episode["id"])
    assert final["status"] == "COMPLETED", final["error"]
    jobs = app.state.store.list("job", episode["id"])
    references = []
    for job in jobs:
        prompt = job["patched_workflow"]["positive"]["inputs"]["text"]
        assert "只有旁白" not in prompt and "旁白脚本独立存储" not in prompt
        assert "positive.text" not in job["ai_parameter_values"]
        video = job["type"] == "SHOT_VIDEO"
        if override:
            assert prompt == ("User video" if video else "User image")
            assert job["parameter_sources"]["positive.text"] == "user"
        elif job["shot_id"]:
            field = {
                "SHOT_START_FRAME": "start_frame_prompt",
                "SHOT_END_FRAME": "end_frame_prompt",
                "SHOT_VIDEO": "video_prompt",
            }[job["type"]]
            assert shot["prompts"][field] in prompt
        else:
            references.append(prompt)
        assert job["patched_workflow"]["sampler"]["inputs"]["denoise"] == (
            0.25 if not job["shot_id"] else 0.6 if video else 0.4
        )
    if not override:
        assert len(set(references)) == len(references) > 1
    assert final["metrics"]["llm_calls"] == episode["metrics"]["llm_calls"]


def test_90_second_legacy_preview_saves_repaired_shots_and_archives_originals(system):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    app.state.generation.provider_factory = CreativeProvider
    episode = preview(system, target_duration=90)
    id = episode["id"]

    def insert_legacy(record):
        for shot in record["shots"]:
            shot["prompts"]["ai_parameters"]["default_image"]["positive.text"] = (
                "" if shot["index"] + 1 in {4, 7, 9} else "可这一次，好奇跑赢了警告——"
            )

    original = app.state.store.update("episode", id, insert_legacy)
    detail = client.get(f"/api/v1/episodes/{id}").json()
    assert app.state.store.get("episode", id) == original
    body = edits(detail)
    body["shots"][-1]["duration"] += 1
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=body).status_code == 400
    assert app.state.store.get("episode", id) == original
    response = client.patch(f"/api/v1/episodes/{id}/preview", json=edits(detail))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["version"] == original["version"] + 1
    assert saved["plan"] == original["plan"] and saved["bible"] == original["bible"]
    assert saved["metrics"] == original["metrics"] and saved["status"] == "AWAITING_REVIEW"
    assert sum(s["duration"] for s in saved["shots"]) == 90
    for old, repaired in zip(original["shots"], saved["shots"], strict=True):
        assert repaired["id"] == old["id"]
        assert repaired["preview_prompt_view"]["values"] == {
            field: old["prompts"][field]
            for field in ("start_frame_prompt", "end_frame_prompt", "video_prompt")
        }
        assert repaired["preview_prompt_view"]["hints"] == {}
        expected = deepcopy(old["prompts"])
        duplicate = expected["ai_parameters"]["default_image"].pop("positive.text")
        assert repaired["prompts"] == expected
        assert repaired["legacy_ai_prompt_parameters"] == {
            "default_image": {"positive.text": duplicate}
        }
    assert client.patch(f"/api/v1/episodes/{id}/preview", json=edits(detail)).status_code == 409
    repeated = client.patch(f"/api/v1/episodes/{id}/preview", json=edits(saved)).json()
    assert repeated["shots"] == saved["shots"]
    assert not comfy.prompts and not app.state.store.list("job", id)


def test_stage_filter_validates_even_discarded_legacy_values(system):
    _, app, _ = system
    profile = app.state.store.get("workflow", "default_image")
    for values in ({"positive.text": None}, {"positive.text": 3}, {"sampler.steps": 1}):
        with pytest.raises(AppError):
            usable_ai_values(profile, values, stage_prompt=True)
    original = {"positive.text": "narration", "negative.text": ""}
    assert usable_ai_values(profile, original, stage_prompt=True) == {"negative.text": ""}
    assert original["positive.text"] == "narration"


def test_reference_prompt_does_not_embed_raw_bible_ai_mapping():
    bible = {"characters": ["tiger"], "ai_parameters": {"image": {"positive.text": "旁白污染"}}}
    assert "旁白污染" not in anchored_prompt(bible, "Three tigers in a forest", {})
    assert "tiger" in anchored_prompt(bible, "Three tigers in a forest", {})
    assert bible["ai_parameters"]["image"]["positive.text"] == "旁白污染"
