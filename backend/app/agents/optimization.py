"""A bounded visual repair proposal, never a new story or executable operation."""

from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from app.agents.schemas import StrictModel
from app.agents.timing import TIMING_HEADER, read_timing
from app.core.errors import AppError

PromptField = Literal["start_frame_prompt", "end_frame_prompt", "video_prompt"]
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class PromptChange(StrictModel):
    prompt: Text = Field(max_length=6000)
    reason: Text = Field(max_length=1000)


class PromptOptimization(StrictModel):
    decision: Literal["revise", "keep", "manual"]
    confidence: Literal["high", "medium", "low"]
    summary: Text = Field(max_length=2000)
    limitations: str = Field(max_length=2000)
    changes: dict[PromptField, PromptChange] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if self.decision == "revise":
            if not self.changes or self.confidence == "low":
                raise ValueError("修正需要明确的修改及足够的图像证据")
        elif self.changes:
            raise ValueError("保留原片或人工处理时不能附带修改")
        return self


def optimization_schema(editable: list[str], *, duration=None, timed_video=False):
    @field_validator("changes")
    @classmethod
    def validate(cls, changes):
        if not set(changes).issubset(editable):
            raise ValueError("不能改写已锁定或当前工作流不使用的提示词")
        if "video_prompt" in changes and duration is not None:
            validate_optimized_timing(changes["video_prompt"].prompt, duration, timed_video)
        return changes

    return create_model(
        "EditablePromptOptimization",
        __base__=PromptOptimization,
        __validators__={"editable_changes": validate},
        changes=(
            dict[PromptField, PromptChange],
            Field(
                default_factory=dict,
                json_schema_extra={
                    # Inline this leaf: Pydantic remaps its internal definition references.
                    "properties": {field: PromptChange.model_json_schema() for field in editable},
                    "additionalProperties": False,
                },
            ),
        ),
    )


def validate_optimized_timing(prompt, duration, required):
    if required and TIMING_HEADER not in prompt:
        raise ValueError("优化已有动作时间线时必须保留时间块")
    read_timing(prompt, duration)


async def optimize_prompts(agents, context: dict, paths: list, editable: list[str]):
    schema = optimization_schema(
        editable,
        duration=context["fixed_duration_seconds"],
        timed_video=TIMING_HEADER in context["effective_prompts"]["video_prompt"],
    )
    result = await agents.generate_json(
        "Act as a visual prompt repair specialist. Observe the supplied actual images first. "
        "frame_order distinguishes timeline samples from generation input keyframes and the prior "
        "clip's end. Compare only the current reviewed target. prior_concerns are hypotheses, "
        "not established facts; verify them independently. Do not infer that a brief action never "
        "occurred simply because it is absent between samples. Never claim to have watched every "
        "frame or heard audio. Use manual with low confidence if motion evidence is insufficient. "
        "Use keep when the alleged problem is not supported and no repair is needed. "
        "For a supported problem, propose minimal, concrete, positive English prompt edits. "
        "Fix video motion using video_prompt while retaining correct input keyframes. "
        "Edit an endpoint prompt only when that actual input image fails its reviewed target. "
        "Keep correct identity, lifecycle, setting, style, framing and unaffected actions. "
        "Respect the fixed duration and workflow capability; one readable primary action per shot. "
        "Clarify action direction and temporal order, remove contradictory or redundant wording. "
        "If the original video prompt contains 'Shot timing (seconds):', keep that section last "
        "using one start-ends: English action line per phase (for example 0-3s: Reach forward.). "
        "Refine the phase actions as needed while covering 0 to fixed_duration_seconds continuously "
        "without gaps or overlaps, max two decimal places. Never silently remove the timeline. "
        "Never remove a required story beat to obtain a better QA score. If the story needs a "
        "different duration, changed action, different workflow or locked input, use manual and "
        "explain the limitation. Do not change narration, story, settings or dynamic AI parameters. "
        "Only use editable_fields; include each changed field's complete replacement prompt and "
        "a specific reason. Do not return unchanged prompts. Summary, reasons and limitations "
        "must be in Chinese, and acknowledge that sampled evidence has limits.",
        context,
        schema,
        images=paths,
    )
    # Enforce at the service boundary even for alternate provider implementations.
    try:
        return schema.model_validate(result.model_dump())
    except ValidationError as exc:
        raise AppError(
            "LLM_INVALID_OUTPUT", "优化结果超出允许修改的字段或格式", status=502
        ) from exc
