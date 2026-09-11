import json
import math
from collections.abc import Callable
from fractions import Fraction

from pydantic import Field, create_model

from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import (
    EpisodePlan,
    PlanBatch,
    QAResult,
    ShotPlan,
    ShotPrompts,
    Transition,
    VisualBible,
)
from app.core.errors import AppError
from app.core.limits import (
    MAX_EPISODE_SHOTS,
    MIN_SHOT_SECONDS,
    PLAN_BATCH_SHOTS,
    PREFERRED_MIN_SHOT_SECONDS,
)


def allocate_centiseconds(
    weights: list[Fraction], target: int, floor: int, limit: int
) -> list[int]:
    """Solve sum(clamp(scale * weight)) exactly, then distribute fractional centiseconds."""
    # Each breakpoint starts or ends one weight's contribution to the slope.
    # Clamping low and high weights simultaneously would discard allocatable time.
    events: dict[Fraction, Fraction] = {}
    for weight in weights:
        for point, change in (
            (Fraction(floor) / weight, weight),
            (Fraction(limit) / weight, -weight),
        ):
            events[point] = events.get(point, Fraction(0)) + change
    amount, slope, previous, scale = (
        Fraction(len(weights) * floor),
        Fraction(0),
        Fraction(0),
        Fraction(0),
    )
    for point, change in sorted(events.items()):
        next_amount = amount + (point - previous) * slope
        if slope and next_amount >= target:
            scale = previous + (target - amount) / slope
            break
        amount, previous, slope = next_amount, point, slope + change
    exact = [min(Fraction(limit), max(Fraction(floor), scale * weight)) for weight in weights]
    result = [math.floor(value) for value in exact]
    remainder = target - sum(result)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - result[i]), i))
    for index in order[:remainder]:
        result[index] += 1
    return result


def normalize_plan(
    plan: EpisodePlan,
    total: float,
    maximum: float,
    *,
    minimum: float = MIN_SHOT_SECONDS,
    first_segment: bool = True,
) -> EpisodePlan:
    """Preserve feasible narrative proportions without favoring earlier shots."""
    count = len(plan.shots)
    target = round(total * 100)
    floor, limit = math.ceil(Fraction(str(minimum)) * 100), int(Fraction(str(maximum)) * 100)
    if floor > limit or count * floor > target or count * limit < target:
        raise AppError(
            "LLM_INVALID_OUTPUT",
            "镜头数量无法满足每镜时长范围及总时长",
            {"min_shots": math.ceil(target / limit), "max_shots": target // floor},
        )
    durations = allocate_centiseconds(
        [Fraction(str(shot.duration)) for shot in plan.shots], target, floor, limit
    )
    result = plan.model_copy(deep=True)
    result.target_duration = total
    for index, shot in enumerate(result.shots):
        shot.index = index
        shot.duration = durations[index] / 100
    if first_segment:
        result.shots[0].transition_from_previous = Transition.ESTABLISHING_CUT
    return result


def segment_timing(total: float, maximum: float, fixed: float | None = None) -> dict:
    """Prefer sustained shots, relaxing only when the segment cannot otherwise fit."""
    target, limit = round(total * 100), int(Fraction(str(maximum)) * 100)
    recommended = math.ceil(target / limit)
    floor = min(round(PREFERRED_MIN_SHOT_SECONDS * 100), target // recommended)
    if fixed is not None:
        floor = limit = round(fixed * 100)
    return {
        "min_shot_duration": max(round(MIN_SHOT_SECONDS * 100), floor) / 100,
        "max_shot_duration": limit / 100,
        "min_shots": recommended,
        "max_shots": min(PLAN_BATCH_SHOTS, target // floor),
        "recommended_shots": recommended,
        "recommended_shot_duration": round(total / recommended, 2),
    }


def planning_schema(base: type[EpisodePlan], timing: dict) -> type[EpisodePlan]:
    shot = create_model(
        "TimedShotPlan",
        __base__=ShotPlan,
        duration=(float, Field(ge=timing["min_shot_duration"], le=timing["max_shot_duration"])),
    )
    return create_model(
        f"Timed{base.__name__}",
        __base__=base,
        shots=(list[shot], Field(min_length=timing["min_shots"], max_length=timing["max_shots"])),
    )


def generation_budget(
    episode: dict, capabilities: dict, system_stats: dict, *, remote_video=False
) -> dict:
    quality = episode["quality"]
    devices = system_stats.get("devices", [])
    available = max(
        (device.get("vram_free", device.get("vram_total", 0)) for device in devices), default=0
    )
    low = episode.get("memory_mode") == "low" or (
        episode.get("memory_mode", "auto") == "auto" and 0 < available < 10 * 1024**3
    )
    short = 384 if low else 512 if quality == "fast" else 720
    ratio = episode["aspect_ratio"]
    width, height = (
        (short, round(short * 16 / 9))
        if ratio == "9:16"
        else (round(short * 16 / 9), short)
        if ratio == "16:9"
        else (short, short)
    )
    width = max(256, round(width / 16) * 16)
    height = max(256, round(height / 16) * 16)
    if low and (
        (episode.get("width") or width) > width or (episode.get("height") or height) > height
    ):
        raise AppError("OVERRIDE_INVALID", "自定义分辨率超过低显存策略上限")
    render_maximum = min(
        capabilities["max_duration"],
        30 if remote_video else 3 if low else 4 if quality == "fast" else 30,
    )
    maximum = min(render_maximum, episode.get("max_shot_duration") or 30)
    return {
        "width": episode.get("width") or width,
        "height": episode.get("height") or height,
        "fps": episode.get("fps", 16),
        "max_duration": maximum,
        "render_max_duration": render_maximum,
        "batch": 1,
        "max_retries": episode.get("max_retries")
        if episode.get("max_retries") is not None
        else {"fast": 1, "standard": 2, "high": 3}[quality],
        "candidates": 2 if quality == "high" else 1,
        "low_memory": low,
        "vram_free": available,
    }


class Directors:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def plan(
        self, episode: dict, maximum: float, *, check_cancel: Callable[[], None] = lambda: None
    ) -> EpisodePlan:
        total = round(episode["target_duration"] * 100)
        limit = int(Fraction(str(maximum)) * 100)
        fixed = episode.get("fixed_shot_duration")
        if fixed is not None:
            fixed_cents = round(fixed * 100)
            if fixed_cents < 100 or fixed_cents > limit or total % fixed_cents:
                raise AppError("OVERRIDE_INVALID", "每镜固定时长需在能力范围内，并整除总时长")
            limit = fixed_cents
        count = math.ceil(total / limit)
        if count > MAX_EPISODE_SHOTS:
            raise AppError(
                "LIMIT_EXCEEDED",
                "当前单镜能力无法在镜头上限内完成目标时长",
                {
                    "required_shots": count,
                    "max_shots": MAX_EPISODE_SHOTS,
                    "max_shot_duration": maximum,
                },
            )
        if count * 100 > total:
            raise AppError("LLM_INVALID_OUTPUT", "总时长无法按每镜至少 1 秒和工作流上限拆分")
        length, remainder = divmod(total, count)
        allocation = [length + (index < remainder) for index in range(count)]
        segments = [
            sum(allocation[index : index + PLAN_BATCH_SHOTS])
            for index in range(0, count, PLAN_BATCH_SHOTS)
        ]
        shots, first = [], None
        start = 0
        for index, segment_duration in enumerate(segments):
            check_cancel()
            seconds = segment_duration / 100
            context = {
                "segment_index": index,
                "segment_count": len(segments),
                "episode_duration": episode["target_duration"],
                "start_time": start / 100,
                "is_final_segment": index == len(segments) - 1,
                "previous_shots": [shot.model_dump(mode="json") for shot in shots[-3:]],
            }
            if first is not None:
                context.update(episode_title=first.title, episode_logline=first.logline)
            batch = await self.plan_segment(
                {**episode, "target_duration": seconds}, limit / 100, context, check_cancel
            )
            check_cancel()
            if first is None:
                first = batch
            for shot in batch.shots:
                shot.index = len(shots)
                shots.append(shot)
            start += round(seconds * 100)
        return EpisodePlan(
            title=first.title,
            logline=first.logline,
            target_duration=episode["target_duration"],
            shots=shots,
        )

    async def plan_segment(
        self, episode: dict, maximum: float, segment: dict, check_cancel
    ) -> EpisodePlan:
        context = {
            key: episode[key] for key in ("idea", "target_duration", "aspect_ratio", "style")
        }
        timing = segment_timing(
            episode["target_duration"], maximum, episode.get("fixed_shot_duration")
        )
        context.update(**timing, **segment)
        schema = planning_schema(PlanBatch if segment["segment_count"] > 1 else EpisodePlan, timing)
        for attempt in range(2):
            check_cancel()
            plan = await self.provider.generate_json(
                "Act as Director. Plan the current time segment of one coherent episode. "
                "Use the entire idea and episode_duration for the overall story arc; keep title/logline about the whole episode. "
                "Continue from previous_shots when present. Only resolve the full story in is_final_segment. "
                "One major visual action per shot. "
                "Choose camera variety and explicit transitions; do not force frame continuation across all shots. "
                f"Timing contract for this segment: each shot must last {timing['min_shot_duration']:g} "
                f"to {timing['max_shot_duration']:g} seconds, inclusive. "
                f"Return {timing['min_shots']} to {timing['max_shots']} shots; "
                f"prefer {timing['recommended_shots']} shots of about {timing['recommended_shot_duration']:g} seconds. "
                f"Their durations must sum to {episode['target_duration']:g} seconds. "
                "These are timeline seconds, not frames or milliseconds. Use at most two decimal places. "
                "Prefer fewer sustained shots with enough time for the action to read. "
                "Do not turn every narrative beat into a separate quick cut. "
                "Add a shot only for an essential change of viewpoint or action, within the timing contract. "
                "Simplify actions to fit the available time; never lower the minimum or raise the maximum.",
                context,
                schema,
            )
            try:
                if not context["min_shots"] <= len(plan.shots) <= context["max_shots"]:
                    raise AppError(
                        "LLM_INVALID_OUTPUT",
                        "单批镜头数量不符合时长约束",
                        {"min_shots": context["min_shots"], "max_shots": context["max_shots"]},
                    )
                return normalize_plan(
                    plan,
                    episode["target_duration"],
                    timing["max_shot_duration"],
                    minimum=timing["min_shot_duration"],
                    first_segment=segment["segment_index"] == 0,
                )
            except AppError as exc:
                if attempt:
                    raise
                context["correction"] = exc.details
        raise AppError("LLM_INVALID_OUTPUT", "无法规划镜头")

    async def bible(self, episode: dict, plan: dict, workflow_parameters=None) -> VisualBible:
        return await self.provider.generate_json(
            "Act as Bible Agent. Specify distinct, stable character identities, environment, visual style, "
            "lighting and continuity rules for this episode only. Preserve the exact subject count in the idea. "
            "Fill ai_parameters[workflow_id][parameter_key] for the listed AI-owned parameters, "
            "respecting their types, bounds and enums. Never fill unlisted workflows or parameters. "
            "Inputs with source=stage_prompt are supplied automatically for each reference image; "
            "do not put them in ai_parameters. Keep narration out of visual descriptions.",
            {
                "idea": episode["idea"],
                "style": episode["style"],
                "plan": plan,
                "workflow_parameters": workflow_parameters or [],
            },
            constrained_output(VisualBible, workflow_parameters or []),
        )

    async def shot(
        self, bible: dict, shot: dict, continuity: dict, workflow_parameters=None, *, idea=""
    ) -> ShotPrompts:
        return await self.provider.generate_json(
            "Act as Shot Agent. Build actionable image/start/end/video/negative prompts. "
            "Inherit the Bible and continuity. End frame is the same subjects, location and lighting seconds later. "
            "Describe one action, camera, light, identity and negative constraints. Prompts should be in English. "
            "Respect the AI-owned workflow parameter types, ranges and enum options. "
            "Fill ai_parameters[workflow_id][parameter_key] for the listed parameters only. "
            "Inputs with source=stage_prompt are supplied automatically from start_frame_prompt, "
            "end_frame_prompt or video_prompt according to the render stage; never put these "
            "prompt-role inputs in ai_parameters. Respect each workflow's media_type and capability. "
            "Visual prompts must describe visible subjects, scene and motion, never spoken narration "
            "or a narrator's voice. If the idea requests narration, put the spoken script only in "
            "narration_text, in the user's language, short enough for this shot's duration. "
            "A silent shot may have empty narration_text but must still have complete visual prompts. "
            "continuity_state describes the expected subject position, direction, environment and time at the end.",
            {
                "idea": idea,
                "bible": bible,
                "shot": shot,
                "continuity": continuity,
                "workflow_parameters": workflow_parameters or [],
            },
            constrained_output(ShotPrompts, workflow_parameters or []),
        )

    async def qa(self, bible: dict, shot: dict, paths: list, stage: str) -> QAResult:
        return await self.provider.generate_json(
            "Act as visual QA. Inspect the supplied actual frames; do not infer success from prompts. "
            "Rate identity/count, scene, style, action, transition and artifacts from 0 to 1. "
            "artifact_score is BAD when high. Explain failures. For keyframes assess the intended endpoints; "
            "for video compare sampled start/middle/end and the prior clip's last frame when supplied.",
            {
                "bible": bible,
                "shot": shot,
                "stage": stage,
                "frame_order": "current start/middle/end; optional previous last frame",
            },
            QAResult,
            images=paths,
        )


def anchored_prompt(bible: dict, prompt: str, continuity: dict) -> str:
    return "\n\n".join(
        [
            "Episode visual bible: "
            + json.dumps(
                {k: v for k, v in bible.items() if k != "ai_parameters"}, ensure_ascii=False
            ),
            prompt,
            "Continuity constraints: " + json.dumps(continuity, ensure_ascii=False),
        ]
    )[:20000]
