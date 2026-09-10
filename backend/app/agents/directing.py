import json
import math
from collections.abc import Callable
from fractions import Fraction

from app.agents.parameters import constrained_output
from app.agents.provider import LLMProvider
from app.agents.schemas import (
    EpisodePlan,
    PlanBatch,
    QAResult,
    ShotPrompts,
    Transition,
    VisualBible,
)
from app.core.errors import AppError
from app.core.limits import MAX_EPISODE_SHOTS, PLAN_BATCH_SHOTS


def normalize_plan(
    plan: EpisodePlan, total: float, maximum: float, *, first_segment: bool = True
) -> EpisodePlan:
    """Allocate centiseconds within bounds, preserving feasible narrative proportions."""
    count = len(plan.shots)
    target, limit = round(total * 100), int(Fraction(str(maximum)) * 100)
    if count * 100 > target or count * limit < target:
        raise AppError(
            "LLM_INVALID_OUTPUT",
            "镜头数量无法满足每镜 1 秒至工作流上限及总时长",
            {"min_shots": math.ceil(target / limit), "max_shots": target // 100},
        )
    durations = [100] * count
    remaining = target - sum(durations)
    while remaining > 0:
        available = [i for i in range(count) if durations[i] < limit]
        weight = sum(plan.shots[i].duration for i in available)
        for i in available:
            extra = min(
                remaining,
                limit - durations[i],
                max(1, int(remaining * plan.shots[i].duration / weight)),
            )
            durations[i] += extra
            remaining -= extra
    result = plan.model_copy(deep=True)
    result.target_duration = total
    for index, shot in enumerate(result.shots):
        shot.index = index
        shot.duration = durations[index] / 100
    if first_segment:
        result.shots[0].transition_from_previous = Transition.ESTABLISHING_CUT
    return result


def generation_budget(episode: dict, capabilities: dict, system_stats: dict) -> dict:
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
    maximum = min(
        capabilities["max_duration"],
        episode.get("max_shot_duration") or 30,
        3 if low else 4 if quality == "fast" else 30,
    )
    return {
        "width": episode.get("width") or width,
        "height": episode.get("height") or height,
        "fps": episode.get("fps", 16),
        "max_duration": maximum,
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
        context.update(
            min_shots=math.ceil(
                round(episode["target_duration"] * 100) / int(Fraction(str(maximum)) * 100)
            ),
            max_shots=min(PLAN_BATCH_SHOTS, math.floor(episode["target_duration"])),
            max_shot_duration=maximum,
            **segment,
        )
        if episode.get("fixed_shot_duration") is not None:
            context["max_shots"] = context["min_shots"]
        for attempt in range(2):
            check_cancel()
            plan = await self.provider.generate_json(
                "Act as Director. Plan the current time segment of one coherent episode. "
                "Use the entire idea and episode_duration for the overall story arc; keep title/logline about the whole episode. "
                "Continue from previous_shots when present. Only resolve the full story in is_final_segment. "
                "One major visual action per shot. "
                "Choose camera variety and explicit transitions; do not force frame continuation across all shots. "
                "Each shot must be at least one second and fit max_shot_duration. Match this segment's target_duration.",
                context,
                PlanBatch if segment["segment_count"] > 1 else EpisodePlan,
            )
            try:
                if len(plan.shots) > context["max_shots"]:
                    raise AppError(
                        "LLM_INVALID_OUTPUT",
                        "单批镜头数量超出上限",
                        {"max_shots": context["max_shots"]},
                    )
                return normalize_plan(
                    plan,
                    episode["target_duration"],
                    maximum,
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
            "respecting their types, bounds and enums. Never fill unlisted workflows or parameters.",
            {
                "idea": episode["idea"],
                "style": episode["style"],
                "plan": plan,
                "workflow_parameters": workflow_parameters or [],
            },
            constrained_output(VisualBible, workflow_parameters or []),
        )

    async def shot(
        self, bible: dict, shot: dict, continuity: dict, workflow_parameters=None
    ) -> ShotPrompts:
        return await self.provider.generate_json(
            "Act as Shot Agent. Build actionable image/start/end/video/negative prompts. "
            "Inherit the Bible and continuity. End frame is the same subjects, location and lighting seconds later. "
            "Describe one action, camera, light, identity and negative constraints. Prompts should be in English. "
            "Respect the AI-owned workflow parameter types, ranges and enum options. "
            "Fill ai_parameters[workflow_id][parameter_key] for the listed parameters only. "
            "continuity_state describes the expected subject position, direction, environment and time at the end.",
            {
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
            "Episode visual bible: " + json.dumps(bible, ensure_ascii=False),
            prompt,
            "Continuity constraints: " + json.dumps(continuity, ensure_ascii=False),
        ]
    )[:20000]
