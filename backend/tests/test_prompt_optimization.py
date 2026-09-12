"""Repair proposals observe real fixture media before any authorized render mutation."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.optimization import PromptOptimization, optimization_schema
from app.agents.schemas import QAResult
from app.core.errors import AppError
from app.generation.prompt_optimization import propose
from app.generation.schemas import PromptOptimizationRequest
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode
from tests.test_capabilities import image_to_video_graph
from tests.test_episode_rerun import complete


def response_data(field="video_prompt", **extra):
    return {
        "decision": "revise",
        "confidence": "high",
        "summary": "输入关键帧符合目标，视频中缺少明确的向前运动。",
        "limitations": "仅有采样图像，不能保证所有动作都已被观察到。",
        "changes": {
            field: {"prompt": f"Corrected {field}: lion walks forward.", "reason": "突出向前动作。"}
        },
        **extra,
    }


class Optimizer(FakeProvider):
    def __init__(self, data=None, *, fail_qa=False):
        super().__init__()
        self.data = data if data is not None else response_data()
        self.calls = []
        self.fail_qa = fail_qa

    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, PromptOptimization):
            self.calls.append((system, deepcopy(context), list(images)))
            assert images and len(images) <= 8 and all(path.is_file() for path in images)
            # Deliberately bypass provider validation to exercise the service boundary too.
            return SimpleNamespace(model_dump=lambda: deepcopy(self.data))
        if schema is QAResult and self.fail_qa:
            return schema.model_validate(
                {
                    "character_consistency": 0.4,
                    "scene_consistency": 0.4,
                    "style_consistency": 0.4,
                    "action_accuracy": 0.4,
                    "transition_quality": 0.4,
                    "artifact_score": 0.5,
                    "explanation": "The fixture remains imperfect after one correction.",
                }
            )
        return await super().generate_json(system, context, schema, images=images)


def draft(client, episode, index=0, **body):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/shots/{episode['shots'][index]['id']}/prompt-optimization",
        json={"expected_version": episode["version"], **body},
    )


def apply(client, episode, proposal, **body):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/prompt-optimizations/{proposal['id']}/apply",
        json={"expected_version": episode["version"], **body},
    )


def install(system, data=None, **options):
    provider = Optimizer(data, **options)
    system[1].state.generation.provider_factory = lambda: provider
    return provider


@pytest.mark.parametrize(
    "data",
    [
        response_data("duration"),
        response_data("end_frame_prompt"),
        response_data(confidence="low"),
        response_data(changes={}),
        response_data(decision="keep"),
        response_data(changes={"video_prompt": {"prompt": "   ", "reason": "invalid"}}),
        response_data(changes={"video_prompt": {"prompt": "x" * 6001, "reason": "invalid"}}),
        response_data(changes={"video_prompt": {"prompt": 4, "reason": "invalid"}}),
        response_data(ai_parameters={"other": {"seed": 1}}),
    ],
)
def test_optimizer_schema_rejects_uneditable_or_invalid_output(data):
    with pytest.raises(ValidationError):
        optimization_schema(["video_prompt"]).model_validate(data)


def test_draft_uses_current_evidence_without_changing_episode_or_media(system):
    client, app, comfy = system
    before = complete(system, target_duration=2, max_shot_duration=1)

    def mark(e):
        shot = e["shots"][1]
        shot["qa"] = [{"explanation": "OLD_UNRELATED_HISTORY"}]
        shot["review_notes"] = [
            {
                "stage": "video",
                "code": "QA_FAILED",
                "message": "CURRENT_CONCERN",
                "asset_ids": [shot["video_asset_id"]],
            },
            {
                "stage": "video",
                "code": "QA_FAILED",
                "message": "STALE_ASSET_CONCERN",
                "asset_ids": ["old-video"],
            },
        ]

    before = app.state.store.update("episode", before["id"], mark)
    provider = install(system)
    jobs = app.state.store.list("job", before["id"])
    assets = app.state.store.list("asset", before["id"])
    submissions = len(comfy.prompts)
    result = draft(client, before, 1, feedback="让角色真正向前走")
    assert result.status_code == 201, result.text
    proposal = result.json()
    assert app.state.store.get("episode", before["id"]) == before
    assert app.state.store.list("job", before["id"]) == jobs
    assert app.state.store.list("asset", before["id"]) == assets
    assert len(comfy.prompts) == submissions
    system_prompt, context, paths = provider.calls[0]
    assert len(paths) == 8
    assert context["frame_order"] == [
        "video_0_percent",
        "video_25_percent",
        "video_50_percent",
        "video_75_percent",
        "video_100_percent",
        "input_start_frame",
        "input_end_frame",
        "previous_last_frame",
    ]
    assert context["user_feedback"] == "让角色真正向前走"
    assert context["fixed_duration_seconds"] == 1
    assert len(context["prior_concerns"]) == 1
    assert "OLD_UNRELATED_HISTORY" not in str(context) and "STALE_ASSET_CONCERN" not in str(context)
    assert "hypotheses" in system_prompt and "insufficient" in system_prompt
    assert "start_frame_prompt" not in context["editable_fields"]
    assert all(not path.exists() for path in paths[:5])
    assert all(path.exists() for path in paths[5:])
    saved = client.get(
        f"/api/v1/episodes/{before['id']}/shots/{before['shots'][1]['id']}/prompt-optimization"
    )
    assert saved.json() == proposal


@pytest.mark.parametrize(
    "field, expected_types",
    [
        ("video_prompt", {"SHOT_VIDEO"}),
        ("end_frame_prompt", {"SHOT_END_FRAME", "SHOT_VIDEO"}),
        ("start_frame_prompt", {"SHOT_START_FRAME", "SHOT_VIDEO"}),
    ],
)
def test_confirm_renders_only_required_media_and_preserves_history(system, field, expected_types):
    client, app, comfy = system
    before = complete(system)
    provider = install(system, response_data(field))
    jobs = {j["id"] for j in app.state.store.list("job", before["id"])}
    original_video = client.get(f"/api/v1/assets/{before['final_video_asset_id']}/file").content
    result = draft(client, before)
    assert result.status_code == 201, result.text
    proposal = result.json()
    accepted = apply(client, before, proposal)
    assert accepted.status_code == 202, accepted.text
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert len(provider.calls) == 1
    new_jobs = [j for j in app.state.store.list("job", before["id"]) if j["id"] not in jobs]
    assert {j["type"] for j in new_jobs} == expected_types
    assert len(new_jobs) == len(expected_types)
    assert after["rerun_history"][-1]["shots"] == before["shots"]
    assert after["rerun_history"][-1]["prompt_optimization"] == proposal
    for key in ("plan", "bible", "references", "idea", "target_duration", "workflow_overrides"):
        assert after[key] == before[key]
    assert after["shots"][0]["duration"] == before["shots"][0]["duration"]
    for key, value in before["shots"][0]["prompts"].items():
        assert after["shots"][0]["prompts"][key] == (
            proposal["changes"][field]["prompt"] if key == field else value
        )
    kind = {
        "video_prompt": "SHOT_VIDEO",
        "end_frame_prompt": "SHOT_END_FRAME",
        "start_frame_prompt": "SHOT_START_FRAME",
    }[field]
    job = next(j for j in new_jobs if j["type"] == kind)
    assert (
        proposal["changes"][field]["prompt"]
        in job["patched_workflow"]["positive"]["inputs"]["text"]
    )
    if field != "start_frame_prompt":
        assert (
            after["shots"][0]["start_frame_asset_id"] == before["shots"][0]["start_frame_asset_id"]
        )
    if field != "end_frame_prompt":
        assert after["shots"][0]["end_frame_asset_id"] == before["shots"][0]["end_frame_asset_id"]
    assert (
        client.get(f"/api/v1/assets/{before['final_video_asset_id']}/file").content
        == original_video
    )
    count = len(comfy.prompts)
    assert apply(client, after, proposal).status_code == 409
    assert len(comfy.prompts) == count


def test_continuity_dependencies_are_previewed_and_only_affected_shots_rerender(system):
    client, app, _ = system
    before = complete(system, target_duration=3, max_shot_duration=1)
    before = app.state.store.update(
        "episode", before["id"], lambda e: e["shots"][2].update(transition_from_previous="CUT")
    )
    install(system)
    proposal = draft(client, before).json()
    assert proposal["affected_shot_ids"] == [s["id"] for s in before["shots"][:2]]
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert after["shots"][2] == before["shots"][2]
    assert after["shots"][1]["prompts"] == before["shots"][1]["prompts"]
    assert (
        after["shots"][1]["start_frame_asset_id"] == after["shots"][0]["actual_end_frame_asset_id"]
    )


@pytest.mark.parametrize("field", ["video_prompt", "start_frame_prompt"])
def test_i2v_optimization_never_renders_end_frames(system, field):
    client, app, _ = system
    graph = image_to_video_graph(client.get("/api/v1/workflows/default_video").json()["workflow"])
    profile = client.post(
        "/api/v1/workflows/import",
        json={"name": "I2V", "capability": "IMAGE_TO_VIDEO", "workflow": graph},
    ).json()
    before = complete(system, video_workflow_id=profile["id"])
    provider = install(system, response_data(field))
    proposal = draft(client, before).json()
    assert "end_frame_prompt" not in provider.calls[0][1]["editable_fields"]
    assert "input_end_frame" not in provider.calls[0][1]["frame_order"]
    assert "end_frame" not in proposal["frames"]
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert after["shots"][0]["end_frame_asset_id"] is None
    assert not any(j["type"] == "SHOT_END_FRAME" for j in app.state.store.list("job", before["id"]))


@pytest.mark.parametrize("decision", ["keep", "manual"])
def test_no_action_recommendations_do_not_allow_rerun(system, decision):
    client, app, comfy = system
    before = complete(system)
    install(system, response_data(decision=decision, changes={}, confidence="low"))
    proposal = draft(client, before).json()
    assert proposal["affected_shot_ids"] == []
    count = len(comfy.prompts)
    assert apply(client, before, proposal).status_code == 400
    assert len(comfy.prompts) == count
    assert app.state.store.get("episode", before["id"]) == before


@pytest.mark.parametrize("mutation", ["episode", "workflow", "busy", "unknown"])
def test_confirmation_rejects_stale_or_unsettled_state(system, mutation):
    client, app, comfy = system
    before = complete(system)
    install(system)
    proposal = draft(client, before).json()
    if mutation == "episode":
        app.state.store.update("episode", before["id"], {"quality": "fast"})
    elif mutation == "workflow":
        profile = app.state.store.get("workflow", before["video_workflow_id"])
        app.state.store.update(
            "workflow", profile["id"], {"configuration_version": profile["version"] + 1}
        )
    elif mutation == "busy":
        app.state.generation.busy.add(before["id"])
    else:
        job = app.state.store.list("job", before["id"])[0]
        app.state.store.update("job", job["id"], {"status": "UNKNOWN"})
    count = len(comfy.prompts)
    current = app.state.store.get("episode", before["id"])
    result = apply(client, current, proposal)
    assert result.status_code == 409, result.text
    assert app.state.store.get("episode", before["id"]) == current
    assert len(comfy.prompts) == count
    app.state.generation.busy.discard(before["id"])


def test_locked_prompt_and_invalid_ai_output_are_rejected_before_render(system):
    client, app, comfy = system
    before = complete(
        system,
        advanced_mode=True,
        workflow_overrides={"default_video": {"positive.text": "Fixed user video prompt"}},
    )
    provider = install(system)
    count = len(comfy.prompts)
    result = draft(client, before)
    assert result.status_code == 502, result.text
    assert "video_prompt" not in provider.calls[0][1]["editable_fields"]
    assert app.state.store.list("prompt_optimization", before["id"]) == []
    assert app.state.store.get("episode", before["id"]) == before
    assert len(comfy.prompts) == count


def test_optimized_shot_keeps_semantic_warning_without_loop_even_in_strict_policy(system):
    client, app, _ = system
    before = complete(system)
    before = app.state.store.update(
        "episode", before["id"], {"qa_enabled": True, "qa_policy": "strict"}
    )
    install(system, fail_qa=True)
    proposal = draft(client, before).json()
    jobs = {j["id"] for j in app.state.store.list("job", before["id"])}
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert after["shots"][0]["needs_review"] is True
    assert after["qa_policy"] == "strict"
    assert len([j for j in app.state.store.list("job", before["id"]) if j["id"] not in jobs]) == 1


def test_model_failure_and_cancel_preserve_originals_and_cleanup_samples(system):
    client, app, _ = system
    before = complete(system)
    captured = []

    class Failing(Optimizer):
        async def generate_json(self, *args, images=None, **kwargs):
            captured.extend(images)
            raise AppError("LLM_TIMEOUT", "fixture timeout")

    app.state.generation.provider_factory = Failing
    assert draft(client, before).status_code == 400
    assert all(not path.exists() for path in captured[:5])
    assert app.state.store.get("episode", before["id"]) == before
    assert not app.state.store.list("prompt_optimization", before["id"])

    class Waiting(Optimizer):
        async def generate_json(self, *args, images=None, **kwargs):
            captured[:] = images
            await asyncio.Event().wait()

    app.state.generation.provider_factory = Waiting

    async def run():
        task = asyncio.create_task(
            propose(
                app.state.generation,
                before["id"],
                before["shots"][0]["id"],
                PromptOptimizationRequest(expected_version=before["version"]),
            )
        )
        for _ in range(300):
            if captured and captured[0].exists():
                break
            await asyncio.sleep(0.01)
        assert captured[0].exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert all(not path.exists() for path in captured[:5])
    assert app.state.store.get("episode", before["id"]) == before


def test_i2i_optimized_tail_edits_original_tail_and_preserves_user_reference_override(system):
    from tests.test_api_capabilities import install_i2i

    client, app, comfy = system
    image = install_i2i(client, comfy)
    before = complete(system, image_workflow_id=image["id"])
    install(system, response_data("end_frame_prompt"))
    proposal = draft(client, before).json()
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    ends = sorted(
        (j for j in app.state.store.list("job", before["id"]) if j["type"] == "SHOT_END_FRAME"),
        key=lambda j: j["created_at"],
    )
    assert ends[-1]["asset_bindings"]["reference_image"] == before["shots"][0]["end_frame_asset_id"]
    assert "targeted edit" in ends[-1]["patched_workflow"]["positive"]["inputs"]["text"]


def test_optimized_video_resumes_unknown_job_without_rewriting_or_duplicate_submission(system):
    import httpx

    client, app, comfy = system
    before = complete(system)
    provider = install(system)
    proposal = draft(client, before).json()
    original = comfy.handle
    state = {"offline": True, "job_id": None}
    comfy.settings.render_timeout = 1

    def handle(request):
        if request.url.path.startswith("/history/"):
            submitted = comfy.prompts.get(request.url.path.rsplit("/", 1)[1])
            if submitted:
                job = app.state.store.get("job", submitted["client_id"])
                if proposal["changes"]["video_prompt"]["prompt"] in job.get("input_values", {}).get(
                    "prompt", ""
                ):
                    state["job_id"] = job["id"]
                    if state["offline"]:
                        raise httpx.ReadTimeout("Interrupted optimization", request=request)
        return original(request)

    comfy.handle = handle
    assert apply(client, before, proposal).status_code == 202
    interrupted = wait_episode(client, before["id"])
    assert interrupted["error"]["code"] == "JOB_TIMEOUT", interrupted.get("error")
    job = app.state.store.get("job", state["job_id"])
    assert job["status"] == "UNKNOWN"
    count = len(comfy.prompts)
    state["offline"] = False
    assert client.post(f"/api/v1/episodes/{before['id']}/generate").status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    recovered = app.state.store.get("job", job["id"])
    assert recovered["comfy_prompt_id"] == job["comfy_prompt_id"]
    assert recovered["patched_workflow"] == job["patched_workflow"]
    assert len(comfy.prompts) == count and len(provider.calls) == 1
    assert len(after["rerun_history"]) == 1


@pytest.mark.parametrize("mode", ["timeout", "disconnect"])
def test_optimization_endpoint_cancels_model_wait_and_cleans_up(system, mode):
    from app.api.routes import propose_prompt_optimization

    client, app, _ = system
    before = complete(system)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    paths = []

    class Slow(Optimizer):
        async def generate_json(self, *args, images=None, **kwargs):
            paths.extend(images)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    app.state.generation.provider_factory = Slow

    async def run():
        async def receive():
            await started.wait()
            if mode == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()

        # One timeout covers sampling and the model; enough time to reach the model.
        if mode == "timeout":
            app.state.config.llm_timeout = 2
        request = SimpleNamespace(app=app, receive=receive)
        with pytest.raises(AppError) as exc:
            await propose_prompt_optimization(
                request,
                before["id"],
                before["shots"][0]["id"],
                PromptOptimizationRequest(expected_version=before["version"]),
            )
        assert exc.value.code == ("LLM_TIMEOUT" if mode == "timeout" else "REQUEST_CANCELLED")
        assert started.is_set() and cancelled.is_set()

    asyncio.run(run())
    assert all(not p.exists() for p in paths[:5])
    assert app.state.store.get("episode", before["id"]) == before
    assert not app.state.store.list("prompt_optimization", before["id"])


def test_change_during_ai_request_discards_proposal(system):
    client, app, _ = system
    before = complete(system)

    class Concurrent(Optimizer):
        async def generate_json(self, *args, **kwargs):
            result = await super().generate_json(*args, **kwargs)
            app.state.store.update("episode", before["id"], {"quality": "high"})
            return result

    app.state.generation.provider_factory = Concurrent
    assert draft(client, before).status_code == 409
    assert not app.state.store.list("prompt_optimization", before["id"])
    after = app.state.store.get("episode", before["id"])
    assert after["shots"] == before["shots"] and after["quality"] == "high"


@pytest.mark.parametrize("mode", ["same_prompt", "foreign_episode", "body_patch"])
def test_invalid_application_cannot_inject_or_reuse_other_episode_data(system, mode):
    client, app, _ = system
    before = complete(system)
    data = response_data()
    if mode == "same_prompt":
        data["changes"]["video_prompt"]["prompt"] = before["shots"][0]["prompts"]["video_prompt"]
    install(system, data)
    result = draft(client, before)
    if mode == "same_prompt":
        assert result.status_code == 400
        return
    proposal = result.json()
    if mode == "foreign_episode":
        other = complete(system)
        assert apply(client, other, proposal).status_code == 404
    else:
        assert (
            apply(client, before, proposal, changes={"video_prompt": "injection"}).status_code
            == 422
        )
    assert app.state.store.get("episode", before["id"]) == before


@pytest.mark.parametrize("previous", [False, True])
async def test_five_point_qa_labels_and_sampling_uncertainty(tmp_path, previous):
    from app.agents.directing import Directors
    from tests.test_qa_context_isolation import RecordingQAProvider

    provider = RecordingQAProvider()
    paths = [tmp_path / f"{i}.png" for i in range(6 if previous else 5)]
    await Directors(provider).qa({}, {"prompts": {}}, paths, "video")
    system_prompt, context, supplied = provider.calls[0]
    expected = ["start_frame", "quarter_frame", "middle_frame", "three_quarter_frame", "end_frame"]
    if previous:
        expected.append("previous_last_frame")
    assert context["frame_order"] == expected and supplied == paths
    assert "uncertain motion evidence" in system_prompt


@pytest.mark.parametrize("user_reference", [False, True])
def test_existing_ai_parameters_and_advanced_overrides_survive_optimization(system, user_reference):
    from tests.test_ai_parameters import configure_ai_parameters

    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    before = complete(
        system,
        advanced_mode=True,
        workflow_overrides={"default_video": {"sampler.denoise": 0.85}} if user_reference else {},
    )

    def ai(e):
        e["shots"][0]["prompts"]["ai_parameters"] = {"default_video": {"sampler.denoise": 0.32}}

    before = app.state.store.update("episode", before["id"], ai)
    install(system)
    proposal = draft(client, before).json()
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    video = next(j for j in app.state.store.list("job", before["id"]) if j["type"] == "SHOT_VIDEO")
    assert video["patched_workflow"]["sampler"]["inputs"]["denoise"] == (
        0.85 if user_reference else 0.32
    )
    assert (
        after["shots"][0]["prompts"]["ai_parameters"]
        == before["shots"][0]["prompts"]["ai_parameters"]
    )


def test_fixed_end_asset_stays_when_start_prompt_changes(system):
    client, app, comfy = system
    fixed = client.post(
        "/api/v1/assets", files={"file": ("fixed.png", comfy.image_bytes, "image/png")}
    ).json()["id"]
    before = complete(
        system, advanced_mode=True, workflow_overrides={"default_video": {"end.image": fixed}}
    )
    install(system, response_data("start_frame_prompt"))
    proposal = draft(client, before).json()
    assert proposal["frames"] == ["start_frame"]
    assert "end_frame_prompt" in proposal["locked_fields"]
    assert apply(client, before, proposal).status_code == 202
    after = wait_episode(client, before["id"])
    assert after["status"] == "COMPLETED", after.get("error")
    assert after["shots"][0]["end_frame_asset_id"] == fixed
    assert not any(j["type"] == "SHOT_END_FRAME" for j in app.state.store.list("job", before["id"]))


def test_old_optimization_reference_cannot_override_later_qa_repair():
    from app.generation.qa_retry import corrected_prompt, correction_reference

    profile = {"capability": "IMAGE_TO_IMAGE", "bindings": {"reference_image": {}}}
    shot = {
        "retry_version": 2,
        "optimization_attempt": {"id": "old", "retry_version": 1},
        "optimization_reference_assets": {"end_frame": "old-tail"},
    }
    assert correction_reference(profile, shot, "end_frame") is None
    assert corrected_prompt("New prompt", shot, "end_frame") == "New prompt"
    shot["qa_retry"] = {"rejected_assets": {"end_frame_asset_id": "new-rejected-tail"}}
    shot["qa_frame_corrections"] = {"end_frame": "Current correction"}
    assert correction_reference(profile, shot, "end_frame") == "new-rejected-tail"


def test_current_strict_qa_result_is_feedback_but_unrelated_history_is_not(system):
    client, app, _ = system
    before = complete(system)
    sid = before["shots"][0]["id"]
    app.state.store.create(
        "qa",
        {
            "shot_id": sid,
            "stage": "video",
            "asset_ids": [before["shots"][0]["video_asset_id"]],
            "result": {"explanation": "Current strict feedback"},
        },
        parent=before["id"],
    )
    app.state.store.create(
        "qa",
        {
            "shot_id": sid,
            "stage": "video",
            "asset_ids": ["different-video"],
            "result": {"explanation": "Wrong asset feedback"},
        },
        parent=before["id"],
    )
    provider = install(system)
    assert draft(client, before).status_code == 201
    context = provider.calls[0][1]
    assert "Current strict feedback" in str(context["prior_concerns"])
    assert "Wrong asset feedback" not in str(context)


@pytest.mark.parametrize("editable", [["video_prompt"], []])
async def test_real_provider_serializes_constrained_optimization_request(tmp_path, editable):
    import json

    import httpx
    from PIL import Image

    from app.agents.directing import Directors
    from app.agents.optimization import optimize_prompts
    from app.agents.provider import LLMProvider
    from app.core.config import Settings

    data = (
        response_data()
        if editable
        else response_data(decision="manual", confidence="low", changes={})
    )
    path = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), "blue").save(path)
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "fixture-vision"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, vlm_model="fixture-vision"), httpx.MockTransport(handle)
    )
    result = await optimize_prompts(
        Directors(provider),
        {
            "frame_order": ["video_0_percent"],
            "fixed_duration_seconds": 1,
            "effective_prompts": {"video_prompt": "Original action"},
        },
        [path],
        editable,
    )
    assert result.model_dump() == data and len(calls) == 1
    schema = optimization_schema(editable).model_json_schema()
    changes = schema["properties"]["changes"]
    assert set(changes["properties"]) == set(editable)
    assert changes["additionalProperties"] is False
    for field in editable:
        assert changes["properties"][field]["properties"]["prompt"]["maxLength"] == 6000
