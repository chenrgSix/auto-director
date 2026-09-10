from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    input: str
    transform: Literal["identity", "duration_to_frames"] = "identity"
    frame_multiple: int = Field(default=4, ge=1, le=32)
    frame_offset: int = Field(default=1, ge=0, le=1)


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supports_start_frame: bool = True
    supports_end_frame: bool = True
    supports_video_reference: bool = False
    supports_multi_reference: bool = False
    max_duration: float = Field(default=5, ge=1, le=30)
    low_memory_workflow_id: str | None = None


class WorkflowImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    type: Literal["image", "video"]
    workflow: dict[str, Any]
    capabilities: Capabilities = Field(default_factory=Capabilities)
    bindings: dict[str, Binding] | None = None
    outputs: dict[str, str] | None = None


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    bindings: dict[str, Binding] | None = None
    outputs: dict[str, str] | None = None
    capabilities: Capabilities | None = None
    parameter_values: dict[str, Any] | None = None
