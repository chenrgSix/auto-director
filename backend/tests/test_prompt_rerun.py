from copy import deepcopy

import pytest

from app.agents.schemas import ShotPrompts
from app.agents.shot_batch import ShotPromptBatch
from app.core.errors import AppError
from tests.fakes import FakeProvider
from tests.test_ai_parameters import configure_ai_parameters
from tests.test_api_pipeline import wait_episode
from tests.test_episode_rerun import complete, rerun


class Rewriter(FakeProvider):
    def __init__(self):
        super().__init__()
        self.contexts = []

    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, ShotPromptBatch):
            return await super().generate_json(system, context, schema, images=images)
        assert issubclass(schema, ShotPrompts), "Rerun must not regenerate the plan or Bible"
        self.contexts.append(deepcopy(context))
        result = await super().generate_json(system, context, schema, images=images)
        data = result.model_dump()
        index = context["shot"]["index"]
        data.update(
            start_frame_prompt=f"Rewritten start {index}",
            end_frame_prompt=f"Rewritten end {index}",
            video_prompt=f"Rewritten video {index}",
            negative_prompt="rewritten negative",
            narration_text=f"新旁白 {index}",
            continuity_state={"direction": f"rewritten-{index}"},
            ai_parameters={},
        )
        for item in context["workflow_parameters"]:
            if item["key"] == "sampler.denoise":
                data["ai_parameters"].setdefault(item["workflow_id"], {})[item["key"]] = 0.32
        return schema.model_validate(data)


@pytest.mark.parametrize("selection", ["first", "first_two", "all"])
def test_prompt_rerun_rewrites_selected_shots_and_preserves_story_and_history(system, selection):
    client, app, _ = system
    before = complete(system, target_duration=3, max_shot_duration=1)

    def old_edits(e):
        e["shots"][2]["transition_from_previous"] = "CUT"
        for shot in e["shots"]:
            shot.update(
                preview_prompt_view={"values": {"start_frame_prompt": "old cached edit"}},
                preview_edited_fields=["start_frame_prompt", "title"],
                legacy_ai_prompt_parameters={"old": {"text": "old duplicate"}},
            )

    before = app.state.store.update("episode", before["id"], old_edits)
    provider = Rewriter()
    app.state.generation.provider_factory = lambda: provider
    count = {"first": 1, "first_two": 2, "all": 3}[selection]
    extra = {} if selection == "all" else {"shot_ids": [s["id"] for s in before["shots"][:count]]}
    after = rerun(client, before, scope="prompts", **extra)
    assert len(provider.contexts) == count
    assert [c["shot"]["index"] for c in provider.contexts] == list(range(count))
    if count > 1:
        assert provider.contexts[1]["continuity"]["direction"] == "rewritten-0"
    for key in ["plan", "bible", "references", "idea", "target_duration"]:
        assert after[key] == before[key]
    history = after["rerun_history"][-1]
    assert history["scope"] == "prompts"
    assert history["shots"] == before["shots"][: max(2, count)]
    for index, (old, shot) in enumerate(zip(before["shots"], after["shots"], strict=True)):
        assert (shot["id"], shot["title"], shot["duration"]) == (
            old["id"],
            old["title"],
            old["duration"],
        )
        if index < count:
            assert shot["prompts"]["video_prompt"] == f"Rewritten video {index}"
            assert "preview_prompt_view" not in shot and "legacy_ai_prompt_parameters" not in shot
            assert shot["preview_edited_fields"] == ["title"]
        else:
            assert shot["prompts"] == old["prompts"]
        if index < max(2, count):
            assert shot["end_frame_asset_id"] != old["end_frame_asset_id"]
            assert shot["video_asset_id"] != old["video_asset_id"]
        else:
            assert shot == old
    assert (
        after["shots"][1]["start_frame_asset_id"] == after["shots"][0]["actual_end_frame_asset_id"]
    )
    for asset in [before["final_video_asset_id"], before["shots"][0]["start_frame_asset_id"]]:
        assert client.get(f"/api/v1/assets/{asset}/file").status_code == 200


@pytest.mark.parametrize("override", [False, True])
def test_rewritten_prompts_and_ai_parameters_reach_patch_respecting_user_override(system, override):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    before = complete(
        system,
        **(
            {
                "advanced_mode": True,
                "workflow_overrides": {
                    "default_image": {"positive.text": "User fixed image", "sampler.denoise": 0.75},
                    "default_video": {"positive.text": "User fixed video", "sampler.denoise": 0.85},
                },
            }
            if override
            else {}
        ),
    )
    old_jobs = {j["id"] for j in app.state.store.list("job", before["id"])}
    provider = Rewriter()
    app.state.generation.provider_factory = lambda: provider
    after = rerun(client, before, scope="prompts")
    jobs = [j for j in app.state.store.list("job", before["id"]) if j["id"] not in old_jobs]
    assert {j["type"] for j in jobs} == {"SHOT_START_FRAME", "SHOT_END_FRAME", "SHOT_VIDEO"}
    for job in jobs:
        video = job["type"] == "SHOT_VIDEO"
        value = job["patched_workflow"]["positive"]["inputs"]["text"]
        if override:
            assert value == ("User fixed video" if video else "User fixed image")
            expected_denoise = 0.85 if video else 0.75
        else:
            field = {
                "SHOT_START_FRAME": "start_frame_prompt",
                "SHOT_END_FRAME": "end_frame_prompt",
                "SHOT_VIDEO": "video_prompt",
            }[job["type"]]
            assert after["shots"][0]["prompts"][field] in value
            expected_denoise = 0.32
        assert job["patched_workflow"]["sampler"]["inputs"]["denoise"] == expected_denoise
    assert after["shots"][0]["prompts"]["narration_text"] == "新旁白 0"
    assert len(provider.contexts) == 1


def test_prompt_generation_failure_keeps_old_history_and_can_resume(system):
    client, app, comfy = system
    before = complete(system)
    count = len(comfy.prompts)
    old_file = client.get(f"/api/v1/assets/{before['final_video_asset_id']}/file").content

    class Failing(Rewriter):
        async def generate_json(self, *args, **kwargs):
            raise AppError("LLM_UNAVAILABLE", "fixture model unavailable")

    app.state.generation.provider_factory = Failing
    response = client.post(
        f"/api/v1/episodes/{before['id']}/rerun",
        json={
            "expected_version": before["version"],
            "scope": "prompts",
        },
    )
    assert response.status_code == 202
    failed = wait_episode(client, before["id"])
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "LLM_UNAVAILABLE"
    assert failed["shots"][0]["prompts"] is None
    assert failed["rerun_history"][-1]["shots"] == before["shots"]
    assert len(comfy.prompts) == count
    assert client.get(f"/api/v1/assets/{before['final_video_asset_id']}/file").content == old_file
    provider = Rewriter()
    app.state.generation.provider_factory = lambda: provider
    assert client.post(f"/api/v1/episodes/{before['id']}/generate").status_code == 202
    resumed = wait_episode(client, before["id"])
    assert resumed["status"] == "COMPLETED", resumed["error"]
    assert len(provider.contexts) == 1 and len(resumed["rerun_history"]) == 1


@pytest.mark.parametrize("setting", [None, False, True])
def test_prompt_rerun_preserves_or_overrides_static_choice(system, setting):
    client, app, _ = system
    before = complete(system)
    before = app.state.store.update(
        "episode",
        before["id"],
        lambda e: e["shots"][0]["prompts"].update(allow_static_end_frame=True),
    )
    app.state.generation.provider_factory = Rewriter
    after = rerun(
        client,
        before,
        scope="prompts",
        **({} if setting is None else {"allow_static_end_frame": setting}),
    )
    assert after["shots"][0]["prompts"]["allow_static_end_frame"] is (
        True if setting is None else setting
    )
    assert after["rerun_history"][-1]["shots"][0]["prompts"]["allow_static_end_frame"] is True


def test_render_failure_resume_reuses_new_prompts_without_another_ai_call(system, monkeypatch):
    client, app, _ = system
    before = complete(system)
    provider = Rewriter()
    app.state.generation.provider_factory = lambda: provider
    render = app.state.generation.render

    async def fail(*args, **kwargs):
        raise AppError("COMFYUI_UNAVAILABLE", "fixture render unavailable")

    monkeypatch.setattr(app.state.generation, "render", fail)
    response = client.post(
        f"/api/v1/episodes/{before['id']}/rerun",
        json={
            "expected_version": before["version"],
            "scope": "prompts",
        },
    )
    assert response.status_code == 202
    failed = wait_episode(client, before["id"])
    assert failed["status"] == "FAILED"
    assert failed["shots"][0]["prompts"]["video_prompt"] == "Rewritten video 0"
    monkeypatch.setattr(app.state.generation, "render", render)
    assert client.post(f"/api/v1/episodes/{before['id']}/generate").status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after["error"]
    assert len(provider.contexts) == 1
    assert after["shots"][0]["prompts"] == failed["shots"][0]["prompts"]
