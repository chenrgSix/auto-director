"""Small ordered prompt batches, sharing the single-shot validation contract."""

import json
from typing import Literal

from pydantic import Field, create_model

from app.agents.audio import H3_INSTRUCTION, audio_output, uses_h3_audio
from app.agents.parameters import constrained_output
from app.agents.schemas import (
    SequenceShotPrompts,
    ShotPlan,
    ShotPrompts,
    StrictModel,
    VisualContinuity,
)
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
        prompts = shot_output(specifications, shot["duration"])
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
            + len(shot_instruction(specifications))
            + 1500
        )
        if selected and (
            size > MAX_BATCH_INPUT_CHARS
            or len(candidate) * (12000 + additional_output) > MAX_BATCH_OUTPUT_ESTIMATE
        ):
            break
        selected = candidate
    return selected


def prompt_model(specifications):
    return (
        SequenceShotPrompts
        if any(item.get("capability") == "REFERENCE_SEQUENCE_TO_VIDEO" for item in specifications)
        else ShotPrompts
    )


def shot_output(specifications, duration):
    base = create_model(
        "ContinuousShotPrompts",
        __base__=prompt_model(specifications),
        visual_continuity=(VisualContinuity, Field()),
    )
    return timed_output(
        audio_output(constrained_output(base, specifications), specifications), duration
    )


def shot_instruction(specifications):
    if prompt_model(specifications) is SequenceShotPrompts:
        return SEQUENCE_INSTRUCTION + (
            " " + H3_INSTRUCTION if uses_h3_audio(specifications) else ""
        )
    return SHOT_INSTRUCTION + (" " + H3_INSTRUCTION if uses_h3_audio(specifications) else "")


SEQUENCE_INSTRUCTION = (
    "Create reference-driven video segments for a continuous camera take. "
    "No generated endpoint images are required: omit image_prompt, start_frame_prompt and end_frame_prompt. "
    "Write a complete English video_prompt with action, camera, lighting and sound. "
    "CONTINUE_FRAME/CONTINUE_VIDEO inherits the preceding segment's visual AND audio motion context; "
    "continue the same action phase without a forced pause, pose reset, repeated opening or ending. "
    "A CUT starts a new take. Never insert an editorial cut or unrelated subject inside a segment. "
    "Use only the visible characters and objects. Declare visual_continuity with scene_id, "
    "visible_character_ids, ordered reference_roles (character:<id>, prop:<id>, environment, style; "
    "at most 9), framing, state_in/state_out and intentional_jump only for a deliberate discontinuity. "
    "Maintain the same canonical state keys and values until an action changes them. "
    "Picture numbering follows this segment's reference_roles order, starting at 1. "
    "References specify identity, environment or objects, not mandatory start or end frames. "
    "Do not use absent characters as reference images. An empty scene must remain unoccupied. "
    "For segments of at least 4 seconds, provide 2-4 action_beats in local seconds, covering the "
    "entire planned duration without gaps or overlaps. Shorter segments may omit beats. "
    "Keep timestamps out of video_prompt; the compiler appends the action beats. "
    "Match the planned action and start/end states without inventing additional plot. "
    "Set continuity_state to the expected action phase, positions and directions at the end. "
    "Only fill the declared AI-owned parameters, never duplicate source=stage_prompt inputs. "
)


SHOT_INSTRUCTION = (
    "Each shot is one continuous camera take for its entire clip. Put editorial cuts and new viewpoints in separate shots, never inside one video prompt. "
    "For an unoccupied insert, explicitly keep people and hands out for the entire duration. "
    "Declare visual_continuity for every shot: stable scene_id, visible_character_ids from the Bible "
    "(empty for unoccupied environment or object inserts), reference_roles in priority order using "
    "character:<id>, prop:<bible.props id>, environment or style, framing, state_in and state_out. The FIRST reference is "
    "the actual source for a single-reference workflow, so never select an unrelated character. "
    "Use its stable prop reference first for a recurring object insert; use environment only when no prop exists; exclude faces and irrelevant furniture from its "
    "image prompts. Keep canonical state keys and values identical across shots in the same scene "
    "until an action changes them: positions, screen directions, prop holders and camera axis. "
    "Read continuity.scene_states across reverse shots; an offscreen character's state persists. "
    "Declare intentional_jump with a reason only for a deliberate discontinuity. CONTINUE_FRAME/VIDEO "
    "reuse the actual preceding frame and require the same scene and framing; use a cut for a new "
    "viewpoint. Include framing and the relevant state_in/state_out directly in the respective "
    "image prompts. Do not paste the entire room layout into a close-up or insert. "
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
    "ai_parameters. Respect each workflow's media_type and capability. Image prompts must "
    "describe visible subjects, scene and motion, never spoken narration or a narrator's voice. "
    "For video without explicit native audio support, if the idea requests narration, "
    "put the spoken script only in narration_text, in the "
    "user's language, short enough for this shot's duration. A silent shot may have empty "
    "narration_text but must still have complete visual prompts. continuity_state describes the "
    "expected subject position, direction, environment and time at the end. "
)
