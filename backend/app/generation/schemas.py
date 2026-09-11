from typing import Any, Literal

from pydantic import Field, model_validator

from app.agents.schemas import StrictModel
from app.core.limits import (
    LEGACY_MAX_SHOTS,
    MAX_DIMENSION,
    MAX_EPISODE_SECONDS,
    MAX_EPISODE_SHOTS,
    MAX_FPS,
    MAX_SHOT_SECONDS,
    MIN_DIMENSION,
    MIN_EPISODE_SECONDS,
)


class EpisodeCreate(StrictModel):
    idea: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(ge=MIN_EPISODE_SECONDS, le=MAX_EPISODE_SECONDS)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    style: str = Field(default="自然纪录片", max_length=200)
    quality: Literal["fast", "standard", "high"] = "standard"
    preview_required: bool = False  # Legacy API clients keep direct generation.
    image_workflow_id: str | None = None
    video_workflow_id: str | None = None
    reference_workflow_id: str | None = None
    advanced_mode: bool = False
    workflow_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    memory_mode: Literal["auto", "low", "manual"] = "auto"
    width: int | None = Field(default=None, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    height: int | None = Field(default=None, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    fps: int = Field(default=16, ge=1, le=MAX_FPS)
    seed: int = Field(default=42, ge=0, le=2147483647)
    max_shot_duration: float | None = Field(default=None, ge=1, le=MAX_SHOT_SECONDS)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    qa_enabled: bool = True
    image_parameters: dict[str, Any] = Field(default_factory=dict)
    video_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_input(self):
        self.idea = self.idea.strip()
        if not self.idea:
            raise ValueError("Idea 不能为空")
        if (self.width is None) != (self.height is None):
            raise ValueError("自定义宽高需要同时提供")
        if self.width and (self.width % 16 or self.height % 16):
            raise ValueError("自定义宽高必须是 16 的倍数")
        legacy = bool(
            self.image_parameters
            or self.video_parameters
            or self.width
            or self.max_shot_duration
            or "fps" in self.model_fields_set
        )
        if "advanced_mode" not in self.model_fields_set and legacy:
            self.advanced_mode = True
        if not self.advanced_mode and (legacy or self.workflow_overrides):
            raise ValueError("参数覆盖需要高级模式")
        return self


class EpisodeWorkflowsUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    image_workflow_id: str = Field(min_length=1)
    video_workflow_id: str = Field(min_length=1)
    reference_workflow_id: str = Field(min_length=1)


class EpisodeWorkflowRestore(StrictModel):
    expected_version: int = Field(ge=1)
    history_revision: int = Field(ge=0)


class EpisodeRerun(StrictModel):
    expected_version: int = Field(ge=1)
    scope: Literal["video", "keyframes"]
    # Omitted means all enabled shots. Never interpret an empty selection as all.
    shot_ids: list[str] | None = Field(default=None, min_length=1, max_length=LEGACY_MAX_SHOTS)
    new_seed: bool = True


class PreviewShotUpdate(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    duration: float = Field(ge=1, le=MAX_SHOT_SECONDS)
    # Whether a prompt is required depends on capability, continuity and explicit inputs.
    start_frame_prompt: str = Field(max_length=6000)
    end_frame_prompt: str = Field(max_length=6000)
    video_prompt: str = Field(max_length=6000)


class PreviewUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    shots: list[PreviewShotUpdate] = Field(min_length=1, max_length=MAX_EPISODE_SHOTS)


class PreviewApproval(StrictModel):
    expected_version: int = Field(ge=1)


class TimelineItem(StrictModel):
    id: str
    enabled: bool = True


class TimelineUpdate(StrictModel):
    # Legacy records may exceed the current plan cap; service only accepts their existing IDs.
    shots: list[TimelineItem] = Field(min_length=1, max_length=LEGACY_MAX_SHOTS)


class TestRun(StrictModel):
    values: dict[str, Any] = Field(default_factory=dict)
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    asset_bindings: dict[str, str] = Field(default_factory=dict)


ACTIVE = {
    "QUEUED",
    "PLANNING",
    "BUILDING_BIBLE",
    "PREPARING_PROMPTS",
    "GENERATING_REFERENCES",
    "GENERATING_KEYFRAMES",
    "RENDERING_VIDEO",
    "QA",
    "COMPOSING",
}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
