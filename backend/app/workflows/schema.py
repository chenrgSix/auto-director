from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.limits import MAX_SHOT_SECONDS, MIN_SHOT_SECONDS


class WorkflowCapability(StrEnum):
    TEXT_TO_IMAGE = "TEXT_TO_IMAGE"
    IMAGE_TO_IMAGE = "IMAGE_TO_IMAGE"
    FIRST_LAST_TO_VIDEO = "FIRST_LAST_TO_VIDEO"
    IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO"
    REFERENCE_SEQUENCE_TO_VIDEO = "REFERENCE_SEQUENCE_TO_VIDEO"

    @property
    def media_type(self) -> str:
        return "image" if self in {self.TEXT_TO_IMAGE, self.IMAGE_TO_IMAGE} else "video"


class ParameterOwner(StrEnum):
    AI = "ai"
    DIRECTOR = "director"
    ASSET_RESOLVER = "asset_resolver"
    SYSTEM = "system"
    WORKFLOW = "workflow"
    USER = "user"


class ParameterRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: ParameterOwner | None = None
    editable: bool = True
    override_policy: Literal["advanced", "never"] = "advanced"


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    optional: bool = False
    node_id: str
    input: str
    transform: Literal["identity", "duration_to_frames"] = "identity"
    frame_multiple: int = Field(default=4, ge=1, le=32)
    frame_offset: int = Field(default=1, ge=0, le=31)
    # Optional model timebase, e.g. native audio/video models fixed at 24 FPS.
    frame_fps: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def validate_frame_clock(self):
        if self.frame_fps is not None and self.transform != "duration_to_frames":
            raise ValueError("固定生成帧率仅用于帧数转换")
        return self


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_prompt_format: Literal["none", "minimax_h3"] = "none"
    supports_start_frame: bool = True
    supports_end_frame: bool = True
    supports_video_reference: bool = False
    supports_multi_reference: bool = False
    max_duration: float = Field(default=5, ge=MIN_SHOT_SECONDS, le=MAX_SHOT_SECONDS)
    low_memory_workflow_id: str | None = None


class WorkflowImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    type: Literal["image", "video"] | None = Field(
        default=None, json_schema_extra={"deprecated": True}
    )
    media_type: Literal["image", "video"] | None = None
    capability: WorkflowCapability | None = None
    workflow: dict[str, Any]
    capabilities: Capabilities = Field(default_factory=Capabilities)
    bindings: dict[str, Binding] | None = None
    outputs: dict[str, str] | None = None
    parameter_rules: dict[str, ParameterRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identity(self):
        media = (
            self.media_type
            or self.type
            or (self.capability.media_type if self.capability else None)
        )
        if self.type and self.media_type and self.type != self.media_type:
            raise ValueError("需要匹配的 media_type / capability（旧 type 仍兼容）")
        if self.capability and self.capability.media_type != media:
            raise ValueError("capability 与 media_type 不匹配")
        if not media:
            raise ValueError("请选择生成用途，或先确认 AI 识别结果")
        self.media_type = self.type = media
        return self


class WorkflowAnalyze(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: dict[str, Any]
    capability: WorkflowCapability | None = None


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    bindings: dict[str, Binding] | None = None
    outputs: dict[str, str] | None = None
    capabilities: Capabilities | None = None
    parameter_values: dict[str, Any] | None = None
    capability: WorkflowCapability | None = None
    media_type: Literal["image", "video"] | None = None
    parameter_rules: dict[str, ParameterRule] | None = None
