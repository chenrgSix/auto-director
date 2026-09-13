from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.agents.schemas import SequenceShotPrompts, ShotPlan, ShotPrompts, StrictModel, VisualBible
from app.core.limits import MAX_EPISODE_SECONDS, MAX_EPISODE_SHOTS, MAX_SHOT_SECONDS
from app.generation.schemas import EpisodeCreate, EpisodeRerun


class CreationBrief(StrictModel):
    idea: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(ge=1, le=MAX_EPISODE_SECONDS)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    style: str = Field(default="自然纪录片", max_length=200)
    quality: Literal["fast", "standard", "high"] = "standard"
    image_review_required: bool = False  # Existing API documents retain automatic production.
    image_workflow_id: str | None = None
    video_workflow_id: str | None = None
    reference_workflow_id: str | None = None
    max_shot_duration: float | None = Field(default=None, ge=1, le=MAX_SHOT_SECONDS)
    seed: int = Field(default=42, ge=0, le=2147483647)
    visual_review: Literal["manual", "model"] = "manual"

    @field_validator("idea")
    @classmethod
    def nonblank_idea(cls, value):
        if not value.strip():
            raise ValueError("创作要求不能为空")
        return value.strip()

    def episode_request(self):
        values = self.model_dump(exclude={"visual_review"}, exclude_none=True)
        return EpisodeCreate(
            **values,
            preview_required=True,
            qa_enabled=True,
            qa_policy="advisory",
        )


class CreationShot(ShotPlan):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    prompts: ShotPrompts | SequenceShotPrompts | None = None


class CreationPackage(StrictModel):
    format: Literal["autodirector.creation/v1"] = "autodirector.creation/v1"
    brief: CreationBrief
    title: str = Field(default="", max_length=200)
    logline: str = Field(default="", max_length=2000)
    bible: VisualBible | None = None
    shots: list[CreationShot] = Field(default_factory=list, max_length=MAX_EPISODE_SHOTS)
    decisions: list[str] = Field(default_factory=list, max_length=50)
    open_questions: list[str] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=10000)


class CreateProject(StrictModel):
    request_id: UUID
    document: CreationPackage


class SaveRevision(CreateProject):
    expected_revision: int = Field(ge=1)


class SubmitRevision(StrictModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    constraints_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConfirmProduction(StrictModel):
    request_id: UUID
    expected_version: int = Field(ge=1)
    confirm: Literal[True]


class RerunProduction(EpisodeRerun):
    request_id: UUID
    confirm: Literal[True]
    scope: Literal["video", "keyframes"]
    shot_ids: list[str] = Field(min_length=1, max_length=MAX_EPISODE_SHOTS)
