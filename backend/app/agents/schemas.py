from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.limits import (
    MAX_EPISODE_SECONDS,
    MAX_EPISODE_SHOTS,
    MAX_SHOT_SECONDS,
    MIN_EPISODE_SECONDS,
    MIN_SHOT_SECONDS,
    PLAN_BATCH_SHOTS,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Transition(StrEnum):
    CUT = "CUT"
    MATCH_CUT = "MATCH_CUT"
    CONTINUE_FRAME = "CONTINUE_FRAME"
    CONTINUE_VIDEO = "CONTINUE_VIDEO"
    ESTABLISHING_CUT = "ESTABLISHING_CUT"
    REACTION_CUT = "REACTION_CUT"
    POV_CUT = "POV_CUT"
    TIME_CUT = "TIME_CUT"


class ShotPlan(StrictModel):
    index: int = Field(ge=0, lt=MAX_EPISODE_SHOTS)
    title: str = Field(min_length=1, max_length=200)
    duration: float = Field(ge=MIN_SHOT_SECONDS, le=MAX_SHOT_SECONDS)
    purpose: str = Field(min_length=1, max_length=1000)
    action: str = Field(min_length=1, max_length=2000)
    camera: str = Field(min_length=1, max_length=1000)
    start_state: str = Field(min_length=1, max_length=2000)
    end_state: str = Field(min_length=1, max_length=2000)
    transition_from_previous: Transition


class EpisodePlan(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    logline: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(ge=MIN_EPISODE_SECONDS, le=MAX_EPISODE_SECONDS)
    shots: list[ShotPlan] = Field(min_length=1, max_length=MAX_EPISODE_SHOTS)


class PlanBatch(EpisodePlan):
    shots: list[ShotPlan] = Field(min_length=1, max_length=PLAN_BATCH_SHOTS)


class CharacterBible(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=2000)
    distinguishing_features: list[str] = Field(max_length=10)


class VisualBible(StrictModel):
    ai_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    characters: list[CharacterBible] = Field(max_length=10)
    environment: dict[str, str]
    style: dict[str, str]
    continuity_rules: list[str] = Field(min_length=1, max_length=30)
    negative_prompt: str = Field(min_length=1, max_length=4000)
    camera_motion: str = Field(min_length=1, max_length=300)
    motion_strength: float = Field(ge=0, le=1)


class ShotPrompts(StrictModel):
    ai_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    allow_static_end_frame: bool = Field(
        default=False,
        strict=True,
        description="True only for an intentional unchanged hold, not a static camera with moving subjects.",
    )
    image_prompt: str = Field(
        min_length=1, max_length=6000, description="Visual image description, never narration."
    )
    start_frame_prompt: str = Field(
        min_length=1, max_length=6000, description="Visual state at the start of this shot."
    )
    end_frame_prompt: str = Field(
        min_length=1,
        max_length=6000,
        description="Visual state at the end, distinct from the start.",
    )
    video_prompt: str = Field(
        min_length=1,
        max_length=6000,
        description="Visible action and camera motion over this shot.",
    )
    narration_text: str = Field(
        default="",
        max_length=2000,
        description=(
            "Optional spoken narration script in the user's language, separate from visual prompts. "
            "Empty for a silent shot or when narration was not requested. This does not render audio."
        ),
    )
    negative_prompt: str = Field(min_length=1, max_length=4000)
    motion_strength: float = Field(ge=0, le=1)
    camera_motion: str = Field(min_length=1, max_length=300)
    continuity_state: dict[str, str] = Field(default_factory=dict)

    @field_validator("image_prompt", "start_frame_prompt", "end_frame_prompt", "video_prompt")
    @classmethod
    def nonblank_visual_prompt(cls, value):
        if not value.strip():
            raise ValueError("Visual prompts must not be blank, even when narration is silent")
        return value


class QAResult(StrictModel):
    character_consistency: float = Field(ge=0, le=1)
    scene_consistency: float = Field(ge=0, le=1)
    style_consistency: float = Field(ge=0, le=1)
    action_accuracy: float = Field(ge=0, le=1)
    transition_quality: float = Field(ge=0, le=1)
    artifact_score: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=3000)
    failed_frames: list[Literal["start_frame", "end_frame"]] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "For keyframe QA, list only the supplied endpoints that actually fail their target. "
            "Leave empty for passing frames and video QA."
        ),
    )
    frame_corrections: dict[
        Literal["start_frame", "end_frame"],
        Annotated[
            str,
            StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1500),
        ],
    ] = Field(
        default_factory=dict,
        description=(
            "English visual corrections for failed_frames only. Describe concrete changes needed "
            "to match the reviewed target, preserving correct identity, framing and setting."
        ),
    )

    @model_validator(mode="after")
    def valid_frame_feedback(self) -> Self:
        if len(self.failed_frames) != len(set(self.failed_frames)):
            raise ValueError("failed_frames must contain unique endpoints")
        if not set(self.frame_corrections).issubset(self.failed_frames):
            raise ValueError("frame_corrections may only address failed_frames")
        return self

    def retry_scope(self, *, high: bool = False) -> str | None:
        floor = 0.85 if high else 0.80
        if min(self.character_consistency, self.scene_consistency, self.style_consistency) < floor:
            return "keyframes"
        if self.transition_quality < (0.8 if high else 0.7):
            return "transition"
        if self.action_accuracy < (0.8 if high else 0.7) or self.artifact_score > (
            0.25 if high else 0.35
        ):
            return "video"
        return None

    def score(self) -> float:
        return (
            self.character_consistency
            + self.scene_consistency
            + self.style_consistency
            + self.action_accuracy
            + self.transition_quality
            + 1
            - self.artifact_score
        ) / 6
