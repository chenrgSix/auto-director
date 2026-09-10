import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import ShotPrompts, VisualBible
from app.core.config import Settings
from app.core.errors import AppError
from app.generation.parameters import ai_parameters, resolve_parameters
from app.workflows.analyzer import patch, validate_ai_parameters
from tests.fakes import FakeProvider
from tests.test_api_pipeline import wait_episode


def specifications():
    return [
        {
            "workflow_id": "workflow-a",
            "key": f"custom.{field}",
            "field": field,
            "owner": "ai",
            "type": kind,
            **constraints,
        }
        for field, kind, constraints in [
            ("amount", "number", {"min": 0, "max": 1}),
            ("count", "integer", {"min": 1, "max": 8}),
            ("enabled", "boolean", {}),
            ("note", "text", {}),
            ("mode", "select", {"enum": ["gentle", "strong"]}),
            ("choice", "select", {"enum": [1, 2]}),
        ]
    ]


async def test_ai_output_schema_scopes_keys_and_exposes_node_constraints():
    schema = constrained_output(ShotPrompts, specifications())
    output = await FakeProvider().generate_json("", {}, schema)
    mapping = schema.model_json_schema()["properties"]["ai_parameters"]
    assert mapping["additionalProperties"] is False
    assert set(mapping["properties"]) == {"workflow-a"}
    workflow = mapping["properties"]["workflow-a"]
    assert workflow["additionalProperties"] is False
    assert workflow["properties"]["custom.amount"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert workflow["properties"]["custom.mode"]["enum"] == ["gentle", "strong"]
    values = {
        "custom.amount": 0.4,
        "custom.count": 2,
        "custom.enabled": True,
        "custom.note": "quiet",
        "custom.mode": "gentle",
        "custom.choice": 1,
    }
    validated = schema.model_validate(
        {**output.model_dump(), "ai_parameters": {"workflow-a": values}}
    )
    assert validated.ai_parameters == {"workflow-a": values}
    assert schema.model_validate(output.model_dump(exclude={"ai_parameters"})).ai_parameters == {}


@pytest.mark.parametrize(
    "mapping",
    [
        {"other-workflow": {"custom.amount": 0.4}},
        {"workflow-a": {"not-listed": 0.4}},
        {"workflow-a": {"custom.amount": "0.4"}},
        {"workflow-a": {"custom.amount": -0.1}},
        {"workflow-a": {"custom.amount": 1.1}},
        {"workflow-a": {"custom.amount": float("nan")}},
        {"workflow-a": {"custom.amount": float("inf")}},
        {"workflow-a": {"custom.count": 2.0}},
        {"workflow-a": {"custom.count": True}},
        {"workflow-a": {"custom.enabled": 1}},
        {"workflow-a": {"custom.note": {"nested": "no"}}},
        {"workflow-a": {"custom.mode": "unlisted"}},
        {"workflow-a": {"custom.choice": True}},
        {"workflow-a": {"custom.amount": None}},
    ],
)
async def test_invalid_ai_output_is_rejected_by_local_schema(mapping):
    schema = constrained_output(ShotPrompts, specifications())
    output = await FakeProvider().generate_json("", {}, schema)
    with pytest.raises(ValidationError):
        schema.model_validate({**output.model_dump(), "ai_parameters": mapping})


async def test_invalid_ai_provider_output_uses_existing_repair_budget():
    schema = constrained_output(ShotPrompts, specifications())
    output = (await FakeProvider().generate_json("", {}, schema)).model_dump()
    output["ai_parameters"] = {"workflow-a": {"custom.count": "2"}}
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    provider = LLMProvider(
        Settings(_env_file=None, llm_model="fixture"), httpx.MockTransport(handle)
    )
    with pytest.raises(AppError) as error:
        await provider.generate_json("", {}, schema)
    assert error.value.code == "LLM_INVALID_OUTPUT" and len(calls) == 2


def configure_ai_parameters(client, comfy):
    comfy.info["KSampler"]["input"]["required"]["denoise"] = ["FLOAT", {"min": 0, "max": 1}]
    for id in ("default_image", "default_video"):
        result = client.patch(
            f"/api/v1/workflows/{id}",
            json={
                "parameter_values": {"sampler.denoise": 0.8},
                "parameter_rules": {"sampler.denoise": {"owner": "ai"}},
            },
        )
        assert result.status_code == 200, result.text


class CreativeProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        result = await super().generate_json(system, context, schema, images=images)
        if issubclass(schema, (VisualBible, ShotPrompts)):
            mapping = {}
            for item in context["workflow_parameters"]:
                if item["key"] == "sampler.denoise":
                    value = (
                        0.25
                        if issubclass(schema, VisualBible)
                        else (0.4 if item["workflow_id"] == "default_image" else 0.6)
                    )
                    mapping.setdefault(item["workflow_id"], {})[item["key"]] = value
            return schema.model_validate({**result.model_dump(), "ai_parameters": mapping})
        return result


@pytest.mark.parametrize("override", [False, True])
def test_ai_owned_parameters_reach_patched_json_with_user_priority(system, override):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    app.state.generation.provider_factory = CreativeProvider
    before = {id: app.state.store.get("workflow", id) for id in ("default_image", "default_video")}
    request = {"idea": "AI dynamic parameters", "target_duration": 1, "aspect_ratio": "1:1"}
    if override:
        request.update(
            advanced_mode=True, workflow_overrides={"default_video": {"sampler.denoise": 0.9}}
        )
    else:
        # A user lock must not block the owning AI in default mode.
        client.patch(
            "/api/v1/workflows/default_video",
            json={"parameter_rules": {"sampler.denoise": {"owner": "ai", "editable": False}}},
        )
        before["default_video"] = app.state.store.get("workflow", "default_video")
    response = client.post("/api/v1/episodes", json=request)
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "COMPLETED", episode["error"]
    jobs = app.state.store.list("job", id)
    assert jobs and {j["workflow_id"] for j in jobs} == set(before)
    for job in jobs:
        reference = job["type"].endswith("_REFERENCE")
        video = job["workflow_id"] == "default_video"
        expected = 0.25 if reference else 0.9 if video and override else 0.6 if video else 0.4
        assert job["patched_workflow"]["sampler"]["inputs"]["denoise"] == expected
        assert job["parameter_sources"]["sampler.denoise"] == (
            "user" if video and override else "ai"
        )
        assert job["ai_parameter_values"]["sampler.denoise"] == (
            0.25 if reference else 0.6 if video else 0.4
        )
        assert comfy.prompts[job["comfy_prompt_id"]]["prompt"] == job["patched_workflow"]
    assert before == {id: app.state.store.get("workflow", id) for id in before}


def test_ai_resolver_rejects_foreign_owner_and_revalidates_before_submission(system):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)
    profile = client.post("/api/v1/workflows/default_image/validate").json()
    before = deepcopy(profile)
    for invalid in (
        {"sampler.steps": 7},
        {"latent.width": 512},
        {"missing.key": 1},
        {"sampler.denoise": 2},
    ):
        with pytest.raises(AppError) as error:
            resolve_parameters(profile, {}, {}, {}, False, ai_values=invalid)
        assert error.value.code == "LLM_INVALID_OUTPUT"
    # Even an invalid value hidden by a legal user override must be rejected.
    with pytest.raises(AppError):
        resolve_parameters(
            profile, {}, {}, {"sampler.denoise": 0.5}, True, ai_values={"sampler.denoise": 2}
        )
    assert all(item["owner"] == "ai" for item in ai_parameters(profile))
    with pytest.raises(AppError):
        validate_ai_parameters(profile, {"sampler.steps": 1})
    values, _, raw, sources = resolve_parameters(
        profile, {"negative": "legacy"}, {}, {}, False, ai_values={"negative.text": "AI negative"}
    )
    assert (
        patch(profile, values, raw, advanced=False, ai_values={"negative.text": "AI negative"})[
            "negative"
        ]["inputs"]["text"]
        == "AI negative"
    )
    assert sources["negative.text"] == "ai" and profile == before
    # ComfyUI changed constraints after job creation; reject before /prompt.
    job = app.state.engine.create_job(
        profile,
        {"prompt": "test"},
        {},
        "workflow-tests",
        None,
        "WORKFLOW_TEST",
        advanced_mode=False,
        ai_values={"sampler.denoise": 0.8},
    )
    comfy.info["KSampler"]["input"]["required"]["denoise"][1]["max"] = 0.5
    with pytest.raises(AppError) as error:
        client.portal.call(app.state.engine.run, job["id"])
    assert error.value.code == "LLM_INVALID_OUTPUT"
    failed = app.state.store.get("job", job["id"])
    assert failed["status"] == "FAILED" and failed["error"]["code"] == "LLM_INVALID_OUTPUT"
    assert not comfy.prompts


@pytest.mark.parametrize("invalid", [{"sampler.denoise": 2}, {"sampler.steps": 7}])
def test_invalid_agent_parameters_fail_episode_before_shot_submission(system, invalid):
    client, app, comfy = system
    configure_ai_parameters(client, comfy)

    class InvalidProvider(FakeProvider):
        async def generate_json(self, system, context, schema, *, images=None):
            output = await super().generate_json(system, context, schema, images=images)
            if not issubclass(schema, ShotPrompts):
                return output
            data = {**output.model_dump(), "ai_parameters": {"default_video": invalid}}
            provider = LLMProvider(
                Settings(_env_file=None, llm_model="fixture"),
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"choices": [{"message": {"content": json.dumps(data)}}]}
                    )
                ),
            )
            return await provider.generate_json(system, context, schema)

    app.state.generation.provider_factory = InvalidProvider
    id = client.post("/api/v1/episodes", json={"idea": "invalid AI", "target_duration": 1}).json()[
        "id"
    ]
    client.post(f"/api/v1/episodes/{id}/generate")
    episode = wait_episode(client, id)
    assert episode["status"] == "FAILED" and episode["error"]["code"] == "LLM_INVALID_OUTPUT"
    jobs = app.state.store.list("job", id)
    assert jobs and all(j["type"].endswith("_REFERENCE") for j in jobs)
