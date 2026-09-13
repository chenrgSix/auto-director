import json
import math
from collections.abc import Callable
from fractions import Fraction

from pydantic import Field, create_model

from app.agents.audio import PERFORMANCE, VISUAL, audio_output
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
from app.agents.shot_batch import batch_context, prompt_batch_schema, shot_instruction, shot_output
from app.agents.timing import TIMING_REQUIRED_SECONDS, compile_timing, timed_output
from app.agents.visual import visual_prose
from app.core.cancellation import run_cancellable
from app.core.errors import AppError
from app.core.limits import (
    MAX_EPISODE_SHOTS,
    MIN_SHOT_SECONDS,
    PLAN_BATCH_SHOTS,
    PREFERRED_MIN_SHOT_SECONDS,
)
from app.workflows.duration import duration_limits


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


def segment_timing(
    total: float,
    maximum: float,
    fixed: float | None = None,
    *,
    available_shots: int = PLAN_BATCH_SHOTS,
) -> dict:
    """Expose feasibility bounds, leaving shot count and pacing to the story."""
    target, limit = round(total * 100), int(Fraction(str(maximum)) * 100)
    floor = round(MIN_SHOT_SECONDS * 100)
    if fixed is not None:
        floor = limit = round(fixed * 100)
    return {
        "min_shot_duration": floor / 100,
        "max_shot_duration": limit / 100,
        "min_shots": math.ceil(target / limit),
        "max_shots": min(PLAN_BATCH_SHOTS, available_shots, target // floor),
        "preferred_min_shot_duration": min(PREFERRED_MIN_SHOT_SECONDS, total, limit / 100),
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


def quality_budget(episode: dict) -> dict:
    quality = episode["quality"]
    return {
        "max_retries": episode.get("max_retries")
        if episode.get("max_retries") is not None
        else {"fast": 1, "standard": 2, "high": 3}[quality],
        "candidates": 2 if quality == "high" else 1,
    }


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
    return {
        "width": episode.get("width") or width,
        "height": episode.get("height") or height,
        "fps": episode.get("fps", 16),
        **duration_limits(episode, capabilities),
        "batch": 1,
        **quality_budget(episode),
        "low_memory": low,
        "vram_free": available,
    }


class Directors:
    def __init__(self, provider: LLMProvider, *, check_cancel: Callable[[], None] | None = None):
        self.provider = provider
        self.check_cancel = check_cancel

    async def generate_json(self, *args, **kwargs):
        if self.check_cancel is None:
            return await self.provider.generate_json(*args, **kwargs)
        return await run_cancellable(
            lambda: self.provider.generate_json(*args, **kwargs), self.check_cancel
        )

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
        # A full 12 * maximum segment would force exactly 12 maximum-length shots.
        # Leave output space for the Director to add shots when the story needs them.
        flexible_count = fixed is None and count < min(MAX_EPISODE_SHOTS, total // 100)
        batch_capacity = max(1, PLAN_BATCH_SHOTS // 2) if flexible_count else PLAN_BATCH_SHOTS
        segments = [
            sum(allocation[index : index + batch_capacity])
            for index in range(0, count, batch_capacity)
        ]
        minimum_counts = [math.ceil(duration / limit) for duration in segments]
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
                "available_shots": MAX_EPISODE_SHOTS
                - len(shots)
                - sum(minimum_counts[index + 1 :]),
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
            episode["target_duration"],
            maximum,
            episode.get("fixed_shot_duration"),
            available_shots=segment["available_shots"],
        )
        context.update(**timing, **segment)
        schema = planning_schema(PlanBatch if segment["segment_count"] > 1 else EpisodePlan, timing)
        pacing = (
            "The user explicitly fixed every shot's duration; honor that exact duration. "
            if episode.get("fixed_shot_duration") is not None
            else (
                "Choose shot count and individual durations from the story's actions, emotional beats, "
                "reveals and changes of viewpoint. The minimum shot count is only a feasibility bound, "
                "not a recommended count; the maximum duration is a ceiling, not a target. "
                "Give each action enough time to read and vary duration when the story needs it. "
                f"For sustained actions, {timing['preferred_min_shot_duration']:g} seconds is a soft "
                "pacing reference, not a minimum: briefer reaction, insert or transition shots are "
                "allowed within the timing contract. Avoid mechanical quick cuts or padding. "
                "Do not force equal durations or artificial alternation; uniform timing is fine "
                "when justified by the story. Explain each shot's narrative role and pacing in purpose. "
            )
        )
        for attempt in range(2):
            check_cancel()
            plan = await self.generate_json(
                "Act as Director. Plan the current time segment of one coherent episode. "
                "Use the entire idea and episode_duration for the overall story arc; keep title/logline about the whole episode. "
                "Continue from previous_shots when present. Only resolve the full story in is_final_segment. "
                "One major visual action per shot. "
                "Choose camera variety and explicit transitions; do not force frame continuation across all shots. "
                f"Timing contract for this segment: each shot must last {timing['min_shot_duration']:g} "
                f"to {timing['max_shot_duration']:g} seconds, inclusive. "
                f"Return {timing['min_shots']} to {timing['max_shots']} shots; "
                f"their durations must sum to {episode['target_duration']:g} seconds. "
                "These are timeline seconds, not frames or milliseconds. Use at most two decimal places. "
                + pacing
                + "Simplify actions to fit the available time; never lower the minimum or raise the maximum.",
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
        return await self.generate_json(
            "Act as Bible Agent. Specify distinct, stable character identities, environment, visual style, "
            "Create props entries with stable IDs and precise shape, material and color for objects recurring across shots. "
            "Keep environment and style entries purely visual: no field headings, sound or performance directions. "
            "lighting and continuity rules for this episode only. Preserve the exact subject count in the idea. "
            "Fill ai_parameters[workflow_id][parameter_key] for the listed AI-owned parameters, "
            "respecting their types, bounds, enums and steps, including every downstream constraint. "
            "Each step sequence starts at that constraint's min (or zero when absent); "
            "the same value must satisfy all constraints. Never fill unlisted workflows or parameters. "
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
        duration = shot["duration"]
        result = await self.generate_json(
            shot_instruction(workflow_parameters or []),
            {
                "idea": idea,
                "bible": bible,
                "shot": shot,
                "continuity": continuity,
                "workflow_parameters": workflow_parameters or [],
                "timing": {
                    "duration_seconds": duration,
                    "required": duration >= TIMING_REQUIRED_SECONDS,
                },
            },
            shot_output(workflow_parameters or [], duration),
        )
        try:
            # Check timing even when a custom provider returns a plain ShotPrompts instance.
            checked = timed_output(
                audio_output(ShotPrompts, workflow_parameters or []), duration
            ).model_validate(result.model_dump())
            return compile_timing(checked)
        except ValueError as exc:
            raise AppError("LLM_INVALID_OUTPUT", "分镜动作时间线格式不正确", status=502) from exc

    async def shot_batch(
        self, bible: dict, shots: list[dict], continuity: dict, workflow_parameters=None, *, idea=""
    ) -> list[ShotPrompts]:
        specifications = workflow_parameters or []
        schema = prompt_batch_schema(shots, specifications)
        result = await self.generate_json(
            shot_instruction(specifications)
            + " Generate the supplied adjacent shots as ONE ordered batch. Return every shot_id "
            "exactly once in input order, using each shot's own fixed duration. Do not merge shots. "
            "For the first shot use the supplied continuity. For each subsequent shot use the "
            "previous item's continuity_state as historical context, then apply the current "
            "shot's planned start/end, time and transition. Do not erase injuries, props or "
            "identity changes without support in the reviewed plan. Keep the stable character IDs "
            "from the Bible in continuity_state. Each prompts object must be complete and concise; "
            "never refer to another item instead of writing a usable visual prompt.",
            batch_context(bible, shots, continuity, specifications, idea),
            schema,
        )
        try:
            # Custom providers must pass the same checks as the HTTP provider.
            checked = schema.model_validate(result.model_dump())
            return [compile_timing(item.prompts) for item in checked.shots]
        except ValueError as exc:
            raise AppError("LLM_INVALID_OUTPUT", "分镜批次格式或参数不正确", status=502) from exc

    async def qa(self, bible: dict, shot: dict, paths: list, stage: str) -> QAResult:
        video_order = (
            [
                "start_frame",
                "quarter_frame",
                "middle_frame",
                "three_quarter_frame",
                "end_frame",
                "previous_last_frame",
            ]
            if len(paths) >= 5
            else ["start_frame", "middle_frame", "end_frame", "previous_last_frame"]
        )
        frame_order = (
            ["start_frame"]
            if stage == "start_candidate"
            else ["start_frame", "end_frame"]
            if stage == "keyframes"
            else video_order
        )[: len(paths)]
        result = await self.generate_json(
            "Act as visual QA. Inspect the supplied actual frames; do not infer success from prompts. "
            "Judge this submission independently using only the supplied images and their reviewed "
            "targets. The target describes the desired result, not evidence of what is visible. "
            "Rate identity/count, scene, style, action and transition from 0 to 1 (higher is better). "
            "For artifact_severity, use the opposite direction: 0 means no visible artifacts, "
            "0.1 means minor artifacts, and 1 means severely corrupted or unusable imagery. "
            "artifact_severity measures visible defects, not quality or confidence; "
            "a clean image needs a value near 0. Explain actual visible failures. "
            "For keyframes assess the intended endpoints "
            "in shot.prompts: near-identical frames fail action/transition when endpoint change is requested. "
            "An empty final scene or departing character can be intentional; follow the current target "
            "over global cast-count or previous-scene constraints. "
            "For keyframes and start_candidate, set failed_frames to only the supplied endpoints that "
            "actually fail their respective reviewed targets. A correct start with an incorrect end "
            "must list only end_frame. Give each failed frame an actionable English frame_corrections "
            "entry: briefly describe the desired visible result when editing this failed image, "
            "preserving correct identity, framing and setting. Prefer concrete positive instructions "
            "over repeating a catalog of defects or unwanted subjects. "
            "Do not invent missing frames or introduce future lifecycle features or other scenes' cast. "
            "Leave both fields empty when the supplied frames pass. For video leave these fields empty "
            "and compare the ordered timeline samples and the prior clip's last frame when supplied. "
            "quarter_frame and three_quarter_frame are 25% and 75% of the timeline. "
            "These are sampled stills, not a full video or audio inspection. Never infer that a brief "
            "action did not occur merely because it is missing between samples. Distinguish visible "
            "contradictions from uncertain motion evidence and explain that uncertainty. Do not penalize "
            "an unobservable action or camera move solely because the sampling cannot verify it.",
            {
                "shot": qa_shot_context(shot),
                "stage": stage,
                "frame_order": frame_order,
            },
            QAResult,
            images=paths,
        )
        editable_frames = set(frame_order) if stage in {"keyframes", "start_candidate"} else set()
        if not set(result.failed_frames).issubset(editable_frames):
            raise AppError(
                "LLM_INVALID_OUTPUT",
                "视觉质检返回了当前阶段未提供的关键帧",
                {"stage": stage, "frame_order": frame_order, "failed_frames": result.failed_frames},
            )
        return result


def qa_shot_context(shot: dict) -> dict:
    """Judge current media against reviewed targets, without prior verdicts or global cast."""
    target = {
        key: shot[key]
        for key in (
            "index",
            "title",
            "duration",
            "actual_duration",
            "purpose",
            "action",
            "camera",
            "start_state",
            "end_state",
            "transition_from_previous",
        )
        if key in shot
    }
    target["timing_policy"] = (
        "Script duration and action timestamps are generation guidance. Review the full generated "
        "video; a different actual duration alone is not a quality failure."
    )
    prompts = shot.get("prompts") or {}
    target["prompts"] = {
        key: prompts[key]
        for key in (
            "image_prompt",
            "start_frame_prompt",
            "end_frame_prompt",
            "video_prompt",
            "negative_prompt",
            "camera_motion",
            "motion_strength",
            "allow_static_end_frame",
            "visual_continuity",
        )
        if key in prompts
    }
    # Saved prompts already select the Bible's identity, lifecycle and style for this shot.
    # Sending all Bible entries again invents requirements from unrelated scenes.
    return target


def anchored_prompt(
    bible: dict, prompt: str, continuity: dict, *, stage: str = "image", visual_continuity=None
) -> str:
    if visual_continuity:
        fields = ["framing", "visible_character_ids"]
        fields += (
            ["state_out"]
            if stage == "end_frame"
            else ["state_in", "state_out"]
            if stage == "video"
            else ["state_in"]
        )
        constraint = "\n\nVisible shot constraints: " + json.dumps(
            {key: visual_continuity[key] for key in fields}, ensure_ascii=False
        )
        if stage == "video" and prompt.startswith(VISUAL):
            # Keep native speech/music and their exact text untouched.
            prompt = prompt.replace(PERFORMANCE, constraint + "\n\n" + PERFORMANCE, 1)
        else:
            prompt += constraint
    if stage == "video" and prompt.startswith(VISUAL):
        return prompt  # Keep the native three-field prompt directly after the frame header.
    # The Shot Agent already selects Bible identity, lifecycle and style for the
    # reviewed target. Replaying the entire catalog here adds unrelated cast and scenes.
    parts = [
        f"Requested {stage} target (highest priority):\n{prompt}",
        (
            "Render the requested END state. When <Picture 1> is supplied, edit it to reach this "
            "target. Preserve the requested identity, framing and background; change pose, position, "
            "setting, lighting or visible subject count only as required by the requested endpoint."
            if stage == "end_frame"
            else "Render only the subjects and setting requested in the target."
        ),
    ]
    # Reference targets are composed from their own Bible entry instead of ShotPrompts.
    if stage == "image":
        parts.append(
            "Visual style, subordinate to the requested target: "
            + visual_prose(bible.get("style", {}))
        )
    return "\n\n".join(parts)[:20000]
