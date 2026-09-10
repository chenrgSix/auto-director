from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
    index: int = Field(ge=0, le=30)
    title: str = Field(min_length=1, max_length=200)
    duration: float = Field(ge=1, le=30)
    purpose: str = Field(min_length=1, max_length=1000)
    action: str = Field(min_length=1, max_length=2000)
    camera: str = Field(min_length=1, max_length=1000)
    start_state: str = Field(min_length=1, max_length=2000)
    end_state: str = Field(min_length=1, max_length=2000)
    transition_from_previous: Transition


class EpisodePlan(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    logline: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(ge=1, le=30)
    shots: list[ShotPlan] = Field(min_length=1, max_length=30)


class CharacterBible(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=2000)
    distinguishing_features: list[str] = Field(max_length=10)


class VisualBible(StrictModel):
    characters: list[CharacterBible] = Field(max_length=10)
    environment: dict[str, str]
    style: dict[str, str]
    continuity_rules: list[str] = Field(min_length=1, max_length=30)


class ShotPrompts(StrictModel):
    image_prompt: str = Field(min_length=1, max_length=6000)
    start_frame_prompt: str = Field(min_length=1, max_length=6000)
    end_frame_prompt: str = Field(min_length=1, max_length=6000)
    video_prompt: str = Field(min_length=1, max_length=6000)
    negative_prompt: str = Field(min_length=1, max_length=4000)
    motion_strength: float = Field(default=0.6, ge=0, le=1)
    camera_motion: str = Field(default="", max_length=300)
    continuity_state: dict[str, str] = Field(default_factory=dict)


class QAResult(StrictModel):
    character_consistency: float = Field(ge=0, le=1)
    scene_consistency: float = Field(ge=0, le=1)
    style_consistency: float = Field(ge=0, le=1)
    action_accuracy: float = Field(ge=0, le=1)
    transition_quality: float = Field(ge=0, le=1)
    artifact_score: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=3000)

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
