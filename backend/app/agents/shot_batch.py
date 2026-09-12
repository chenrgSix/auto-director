"""Small ordered prompt batches, sharing the single-shot validation contract."""

import json
from typing import Literal

from pydantic import Field, create_model

from app.agents.parameters import constrained_output
from app.agents.schemas import ShotPlan, ShotPrompts, StrictModel
from app.agents.timing import timed_output

# Planning estimates, not provider token limits. Oversized requests use the legacy single call.
MAX_BATCH_INPUT_CHARS = 48000
MAX_BATCH_OUTPUT_ESTIMATE = 60000


class PromptBatchItem(StrictModel):
    shot_id: str
    prompts: ShotPrompts


class ShotPromptBatch(StrictModel):
    shots: list[PromptBatchItem]


def prompt_batch_schema(shots, specifications):
    entries = []
    for index, shot in enumerate(shots):
        prompts = timed_output(constrained_output(ShotPrompts, specifications), shot["duration"])
        entries.append(
            create_model(
                f"PromptBatchItem{index}",
                __base__=PromptBatchItem,
                shot_id=(Literal[shot["id"]], ...),
                prompts=(prompts, ...),
            )
        )
    # A fixed tuple validates order, exact IDs/count, and each shot's own duration/schema.
    return create_model(
        "ScopedShotPromptBatch",
        __base__=ShotPromptBatch,
        shots=(tuple[tuple(entries)], Field()),
    )


def batch_context(bible, shots, continuity, specifications, idea):
    return {
        "idea": idea,
        "bible": bible,
        "shots": [
            {"id": shot["id"], **{key: shot[key] for key in ShotPlan.model_fields}}
            for shot in shots
        ],
        "continuity": continuity,
        "workflow_parameters": specifications,
    }


def select_prompt_batch(bible, pending, continuity, specifications, idea, maximum):
    selected = []
    additional_output = sum(
        (1000 if item.get("role") in {"negative", "camera_motion"} else 6000)
        if item["type"] in {"text", "textarea"}
        else 100
        for item in specifications
        if item.get("owner") == "ai" and item.get("role") != "prompt"
    )
    for shot in pending[:maximum]:
        if shot.get("prompts"):
            break
        candidate = [*selected, shot]
        context = batch_context(bible, candidate, continuity, specifications, idea)
        size = (
            len(json.dumps(context, ensure_ascii=False))
            + len(json.dumps(prompt_batch_schema(candidate, specifications).model_json_schema()))
            + len(SHOT_INSTRUCTION)
            + 1500
        )
        if selected and (
            size > MAX_BATCH_INPUT_CHARS
            or len(candidate) * (12000 + additional_output) > MAX_BATCH_OUTPUT_ESTIMATE
        ):
            break
        selected = candidate
    return selected


SHOT_INSTRUCTION = (
    "Act as Shot Agent. Build actionable image/start/end/video/negative prompts. Use the Bible "
    "for stable identity and style. Follow this shot's start_state and end_state; previous "
    "continuity is historical context, not a requirement to repeat the previous scene. Describe "
    "the visible endpoint changes explicitly, including departures, pose, location and time. Do "
    "not force every character to appear in every frame. Select only identity traits visible at "
    "this shot's lifecycle stage; omit traits from earlier or future stages, absent characters "
    "and props or lighting from other scenes. Include the relevant identity and style directly "
    "in each visual prompt; renders use these reviewed prompts. Set allow_static_end_frame=true "
    "only for an intentional freeze or unchanged hold in the shot plan, never merely because "
    "the camera is static or motion is small. Otherwise keep it false. Describe one action, "
    "camera, light, identity and negative constraints. Prompts should be in English. Match "
    "action complexity to this shot's fixed duration and workflow capability. Prioritize one "
    "readable primary action and a feasible camera move; do not pack a short shot with multiple "
    "sequential actions, viewpoint changes and an additional ending effect. Preserve required "
    "start/end story beats, clarify their temporal order and remove redundant or contradictory "
    "embellishments rather than adding more instructions. For shots of 4 seconds or longer, "
    "give 2-4 action_beats with explicit local start/end seconds, continuously covering the "
    "entire fixed duration. Shorter shots may omit beats for one clear action. Allocate time by "
    "action needs, not equal divisions. For example a 5s reach could use 0-3s extending the "
    "hand towards a cup, then 3-5s grasping and lifting it. This is a timing example, never add "
    "a cup to an unrelated story. Break down the planned primary action, not new plot events. "
    "Each phase describes visible subject movement and compatible camera motion; the last "
    "reaches end_state. Even an intentional hold can describe sustained stillness in phases "
    "without inventing motion. No mandatory pause, cut or camera reset at phase boundaries. "
    "Keep video_prompt a concise visual overview without timestamps; action_beats will be "
    "appended automatically. Never put timing sections in the single-image endpoint prompts. "
    "Respect the AI-owned workflow parameter types, ranges, enum options and steps, including "
    "every downstream constraint. Each step sequence starts at that constraint's min (or zero "
    "when absent); the same value must satisfy all constraints. Fill "
    "ai_parameters[workflow_id][parameter_key] for the listed parameters only. Inputs with "
    "source=stage_prompt are supplied automatically from start_frame_prompt, end_frame_prompt "
    "or video_prompt according to the render stage; never put these prompt-role inputs in "
    "ai_parameters. Respect each workflow's media_type and capability. Visual prompts must "
    "describe visible subjects, scene and motion, never spoken narration or a narrator's voice. "
    "If the idea requests narration, put the spoken script only in narration_text, in the "
    "user's language, short enough for this shot's duration. A silent shot may have empty "
    "narration_text but must still have complete visual prompts. continuity_state describes the "
    "expected subject position, direction, environment and time at the end. "
)
