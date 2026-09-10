from typing import Any, Literal

from pydantic import Field, model_validator

from app.agents.schemas import StrictModel
from app.core.duration import MAX_EPISODE_SECONDS, MAX_EPISODE_SHOTS, MIN_EPISODE_SECONDS


class EpisodeCreate(StrictModel):
    idea: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(ge=MIN_EPISODE_SECONDS, le=MAX_EPISODE_SECONDS)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    style: str = Field(default="自然纪录片", max_length=200)
    quality: Literal["fast", "standard", "high"] = "standard"
    image_workflow_id: str | None = None
    video_workflow_id: str | None = None
    memory_mode: Literal["auto", "low", "manual"] = "auto"
    width: int | None = Field(default=None, ge=256, le=2048)
    height: int | None = Field(default=None, ge=256, le=2048)
    fps: int = Field(default=16, ge=1, le=60)
    seed: int = Field(default=42, ge=0, le=2147483647)
    max_shot_duration: float | None = Field(default=None, ge=1, le=30)
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
        return self


class TimelineItem(StrictModel):
    id: str
    enabled: bool = True


class TimelineUpdate(StrictModel):
    shots: list[TimelineItem] = Field(min_length=1, max_length=MAX_EPISODE_SHOTS)


class TestRun(StrictModel):
    values: dict[str, Any] = Field(default_factory=dict)
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    asset_bindings: dict[str, str] = Field(default_factory=dict)


ACTIVE = {
    "QUEUED",
    "PLANNING",
    "BUILDING_BIBLE",
    "GENERATING_REFERENCES",
    "GENERATING_KEYFRAMES",
    "RENDERING_VIDEO",
    "QA",
    "COMPOSING",
}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
