"""Generate constrained action beats, then keep the editable video prompt as the only source."""

import re
from decimal import ROUND_HALF_UP, Decimal

from pydantic import Field, create_model, field_validator, model_validator

from app.agents.schemas import ShotPrompts, StrictModel

TIMING_HEADER = "Shot timing (seconds):"
TIMING_REQUIRED_SECONDS = 4
CENT = Decimal("0.01")
LINE = re.compile(r"(\d+(?:\.\d{1,2})?)-(\d+(?:\.\d{1,2})?)s: (\S.*)")


class ActionBeat(StrictModel):
    start: float = Field(ge=0, strict=True)
    end: float = Field(gt=0, strict=True)
    action: str = Field(min_length=1, max_length=1000, strict=True)

    @field_validator("action")
    @classmethod
    def one_line_action(cls, value):
        if not value.strip() or len(value.splitlines()) != 1 or TIMING_HEADER in value:
            raise ValueError("动作阶段须为非空的单行可见动作描述")
        return value.strip()


def seconds(value):
    return Decimal(str(value))


def validate_beats(beats, duration):
    cursor = Decimal(0)
    for beat in beats:
        start, end = seconds(beat.start), seconds(beat.end)
        if start.as_tuple().exponent < -2 or end.as_tuple().exponent < -2:
            raise ValueError("动作时间最多保留两位小数")
        if start != cursor or end <= start or end > seconds(duration):
            raise ValueError("动作时间须从 0 开始，连续无重叠且不超出镜头时长")
        cursor = end
    if cursor != seconds(duration):
        raise ValueError(f"动作时间须完整覆盖本镜 {duration} 秒")
    return beats


def timed_output(base, duration):
    """Only new Shot Agent outputs require beats; saved legacy ShotPrompts remain valid."""
    required = duration >= TIMING_REQUIRED_SECONDS
    beat_schema = create_model(
        "ShotActionBeat",
        __base__=ActionBeat,
        start=(float, Field(ge=0, lt=duration, strict=True, multiple_of=0.01)),
        end=(float, Field(gt=0, le=duration, strict=True, multiple_of=0.01)),
    )

    @field_validator("action_beats")
    @classmethod
    def check(cls, beats):
        return validate_beats(beats, duration) if beats else beats

    @model_validator(mode="after")
    def compilable(self):
        compile_timing(self)
        return self

    return create_model(
        f"Timed{base.__name__}",
        __base__=base,
        __validators__={"check_action_beats": check, "compilable_timing": compilable},
        action_beats=(
            list[beat_schema],
            Field(
                **({} if required else {"default_factory": list}),
                min_length=2 if required else 0,
                max_length=4 if required else 2,
                description=(
                    f"Local seconds within this {duration}s shot: contiguous from 0 to {duration}, "
                    "no gaps/overlap, max two decimal places. Split the SAME primary action into "
                    "readable phases, preserving the planned start/end; do not invent extra events. "
                    "Each English action specifies visible change and feasible camera motion. "
                    + ("Use 2-4 phases." if required else "May be empty for a short single action.")
                ),
            ),
        ),
    )


def number(value):
    return (
        format(seconds(value).quantize(CENT, rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")
    )


def compose_timing(overview, beats):
    return (
        overview.rstrip()
        + "\n\n"
        + TIMING_HEADER
        + "\n"
        + "\n".join(f"{number(beat.start)}-{number(beat.end)}s: {beat.action}" for beat in beats)
    )


def compile_timing(result):
    data = result.model_dump()
    beats = data.pop("action_beats", [])
    if TIMING_HEADER in data["video_prompt"]:
        raise ValueError("video_prompt 只写画面概述，动作时间须填写 action_beats")
    if beats:
        data["video_prompt"] = compose_timing(
            data["video_prompt"], [ActionBeat.model_validate(b) for b in beats]
        )
    # Enforce the same final 6000-character prompt limit, including the timing section.
    return ShotPrompts.model_validate(data)


def read_timing(prompt, duration=None):
    if TIMING_HEADER not in prompt:
        return None
    if prompt.count(TIMING_HEADER) != 1:
        raise ValueError("视频提示词只能包含一份动作时间线")
    overview, block = prompt.split(TIMING_HEADER)
    beats = []
    for line in block.strip().splitlines():
        match = LINE.fullmatch(line.strip())
        if not match:
            raise ValueError("动作时间格式须为 0-3s: 动作，时间线放在视频提示词末尾")
        start, end, action = match.groups()
        beats.append(ActionBeat(start=float(start), end=float(end), action=action))
    if not beats:
        raise ValueError("动作时间线不能为空")
    validate_beats(beats, duration if duration is not None else beats[-1].end)
    return overview.rstrip(), beats


def retime_prompt(prompt, old_duration, new_duration):
    timing = read_timing(prompt, old_duration)
    if not timing or old_duration == new_duration:
        return prompt
    overview, beats = timing
    ratio = seconds(new_duration) / seconds(old_duration)
    scaled = [
        ActionBeat(
            start=float((seconds(b.start) * ratio).quantize(CENT, rounding=ROUND_HALF_UP)),
            end=float((seconds(b.end) * ratio).quantize(CENT, rounding=ROUND_HALF_UP)),
            action=b.action,
        )
        for b in beats
    ]
    validate_beats(scaled, new_duration)
    return compose_timing(overview, scaled)


def segment_prompt(prompt, duration, start, end):
    timing = read_timing(prompt, duration)
    if not timing:
        return prompt
    overview, beats = timing
    # Segment cuts can fall between beats and between centiseconds (e.g. 5 / 3).
    left = seconds(start).quantize(CENT, rounding=ROUND_HALF_UP)
    right = seconds(end).quantize(CENT, rounding=ROUND_HALF_UP)
    clipped = [
        ActionBeat(
            start=float(max(seconds(b.start), left) - left),
            end=float(min(seconds(b.end), right) - left),
            action=b.action,
        )
        for b in beats
        if seconds(b.end) > left and seconds(b.start) < right
    ]
    return compose_timing(
        overview + "\nRender only this segment's phases; continue from the supplied start image.",
        clipped,
    )
