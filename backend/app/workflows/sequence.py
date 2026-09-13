"""MiniMax Director adapter: compile trusted segment intent, never reuse canvas state."""

import json
import re
from copy import deepcopy
from pathlib import PurePosixPath

from pydantic import Field

from app.agents.schemas import StrictModel
from app.core.errors import AppError
from app.workflows.frame_timing import frame_count

CAPABILITY = "REFERENCE_SEQUENCE_TO_VIDEO"
MAX_SEGMENTS = 8
MAX_REFERENCES = 9


def is_sequence(profile):
    return profile.get("capability") == CAPABILITY


class SequenceSegment(StrictModel):
    shot_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=12000)
    duration: float = Field(ge=1, le=5)
    references: list[str] = Field(min_length=1, max_length=MAX_REFERENCES)


class SequenceInput(StrictModel):
    segments: list[SequenceSegment] = Field(min_length=1, max_length=MAX_SEGMENTS)


def sequence_input(values):
    try:
        result = SequenceInput.model_validate(values.get("_sequence"))
        ids = [segment.shot_id for segment in result.segments]
        if len(set(ids)) != len(ids):
            raise ValueError("片段身份重复")
        for segment in result.segments:
            if not segment.prompt.strip() or len(set(segment.references)) != len(
                segment.references
            ):
                raise ValueError("片段提示词为空或参考槽位重复")
            if any(not re.fullmatch(r"reference_image(?:_\d+)?", r) for r in segment.references):
                raise ValueError("片段需要明确的图片参考槽位")
        return result
    except ValueError as exc:
        raise AppError(
            "SEQUENCE_INVALID", "连续镜头组不符合制作约束", str(exc)[:1000], 422
        ) from exc


def validate_sequence_profile(profile):
    graph, bindings = profile["workflow"], profile.get("bindings", {})
    errors = []

    def fail(message):
        errors.append({"code": "WORKFLOW_INVALID", "message": message})

    binding = bindings.get("sequence", {})
    node_id = binding.get("node_id")
    node = graph.get(node_id, {})
    if node.get("class_type") != "MiniMaxH3Director" or binding.get("input") != "timeline_data":
        fail("连续镜头需要 sequence 绑定到 MiniMaxH3Director.timeline_data")
    if sum(n["class_type"] == "MiniMaxH3Director" for n in graph.values()) != 1:
        fail("连续镜头模板需要且只能包含一个导演台")
    fields = {
        "prompt": "global_prompt",
        "duration": "total_frames",
        "width": "width",
        "height": "height",
        "fps": "frame_rate",
        "seed": "seed",
    }
    for role, field in fields.items():
        if (
            bindings.get(role, {}).get("node_id") != node_id
            or bindings.get(role, {}).get("input") != field
        ):
            fail(f"连续镜头的 {role} 必须绑定到同一导演台的 {field}")
    if any(role in bindings for role in ("start_frame", "end_frame")):
        fail("连续镜头由参考素材驱动，不接受逐镜首尾帧绑定")
    duration = bindings.get("duration", {})
    if (
        duration.get("transform"),
        duration.get("frame_multiple"),
        duration.get("frame_offset"),
        duration.get("frame_fps"),
    ) != ("duration_to_frames", 17, 5, 24):
        fail("当前导演台适配要求每段使用 24 FPS / 17n+5 帧时钟")
    if profile["capabilities"].get("max_duration", 0) > 5:
        fail("当前连续镜头适配每段最多规划 5 秒")
    report = graph.get(profile.get("outputs", {}).get("sequence_report"), {})
    if report.get("class_type") != "PreviewAny" or report.get("inputs", {}).get("source") != [
        node_id,
        5,
    ]:
        fail("连续镜头需要 sequence_report 输出完整导演台报告")
    video = graph.get(profile.get("outputs", {}).get("video"), {})
    if video.get("class_type") != "VHS_VideoCombine" or any(
        video.get("inputs", {}).get(field) != [node_id, slot]
        for field, slot in (("images", 0), ("audio", 1), ("frame_rate", 2))
    ):
        fail("连续镜头输出必须同时接收导演台的完整画面、声音和实际帧率")
    return errors


def relative_image_path(value):
    value = str(value).replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value or "\x00" in value:
        raise AppError("INVALID_MEDIA", "参考图必须是 ComfyUI input 下的相对路径")
    return path.as_posix()


def compile_sequence(profile, graph, values, uploaded):
    source = sequence_input(values)
    result = deepcopy(graph)
    node_id = profile["bindings"]["sequence"]["node_id"]
    inputs = result[node_id]["inputs"]
    width, height, fps = inputs["width"], inputs["height"], inputs["frame_rate"]
    if fps != 24:
        raise AppError("SEQUENCE_INVALID", "连续镜头当前要求 24 FPS")
    segments, cursor = [], 0
    for index, segment in enumerate(source.segments):
        refs = []
        for slot, key in enumerate(segment.references):
            if not uploaded.get(key):
                raise AppError(
                    "ASSET_REQUIRED",
                    "连续片段缺少参考图片",
                    {"shot_id": segment.shot_id, "role": key},
                )
            relative = relative_image_path(uploaded[key])
            refs.append(
                {"index": slot, "imageFile": relative, "fileName": PurePosixPath(relative).name}
            )
        count = frame_count(profile["bindings"]["duration"], segment.duration, fps)
        segments.append(
            {
                "id": segment.shot_id,
                "start": cursor,
                "length": count,
                "frameCount": count,
                "durationSec": count / fps,
                "prompt": segment.prompt,
                "taskType": "r2v",
                "refs": refs,
                "refAudios": [],
                "refVideos": [],
                "negativePrompt": "",
                "continuityFromPrev": index > 0,
                "refImageSize": "match",
            }
        )
        cursor += count
    common = {"commonEnabled": False, "prompt": "", "refs": [], "refAudios": [], "refVideos": []}
    timeline = {
        "version": 5,
        "editMode": "segment",
        "timelineMode": "prompt_batch",
        "totalFrames": cursor,
        "frameRate": fps,
        "width": width,
        "height": height,
        "refMaxSize": inputs["ref_max_size"],
        "durationSec": cursor / fps,
        "video": {"frames": [], "frameMap": [], "sourceFrameCount": cursor},
        "videoClips": [],
        "keyframes": [],
        "shots": [],
        "segments": segments,
        "global": common,
        "globalCommon": common,
        "batchWorkspaces": {
            "r2v": {"segments": segments, "globalCommon": common, "selectedIndex": 0}
        },
        "runSelectEnabled": False,
        "runSelection": [],
        "liveTaePreview": False,
        "gen": {"defaultFrameCount": segments[0]["frameCount"]},
        "output": {
            "mode": "fixed",
            "width": width,
            "height": height,
            "maxExportFrames": 0,
            "exportMode": "all",
            "audioMode": "generate",
            "exportSourceImages": False,
            "continuityEnabled": True,
            "continuityOverlapFrames": 22,
            "continuityKeepTail": True,
        },
    }
    # All creative content is scoped per segment; canvas state and stale Picture hints are discarded.
    inputs.update(
        timeline_data=json.dumps(timeline, ensure_ascii=False, separators=(",", ":")),
        total_frames=cursor,
        global_prompt="",
        task_type="r2v — 参考主体生视频(Reference to Video)",
        export_source_images=False,
    )
    for name in ("i2v_groups", "r2v_groups", "refine"):
        if name in inputs:
            raise AppError("SEQUENCE_INVALID", "首版连续镜头不支持外部素材组或 Refine 覆盖")
    output = result[profile["outputs"]["video"]]["inputs"]
    output.update(trim_to_audio=False, pingpong=False, loop_count=0, save_output=True)
    return result


def exported_lengths(profile, values, history, actual_frames):
    """Fail closed on an incompatible Director report; never estimate cut points by seconds."""
    source = sequence_input(values)
    output = history.get("outputs", {}).get(profile["outputs"]["sequence_report"], {})
    report = "\n".join(v for v in output.get("text", []) if isinstance(v, str))
    lengths = []
    for index, segment in enumerate(source.segments, 1):
        pattern = rf"^Segment {index}/{len(source.segments)}: r2v .*?\(~(\d+) ref image\(s\), 0 ref video\(s\)\)"
        match = re.search(pattern, report, re.MULTILINE)
        if not match or int(match[1]) != len(segment.references):
            raise AppError(
                "SEQUENCE_REPORT_INVALID", "导演台报告未确认实际参考数量", {"segment": index}
            )
        if index == 1:
            length = frame_count(profile["bindings"]["duration"], segment.duration, 24)
        else:
            guide = re.search(
                rf"^Seg #{index}: continuity guide — 22f from seg #{index - 1} \(AV latent, \+audio\); sample=(\d+)f → export (\d+)f",
                report,
                re.MULTILINE,
            )
            if not guide or int(guide[1]) - 22 != int(guide[2]):
                raise AppError(
                    "SEQUENCE_REPORT_INVALID", "导演台未确认有效 AV 接续帧数", {"segment": index}
                )
            length = int(guide[2])
        lengths.append(length)
    if "Partial run:" in report or sum(lengths) != actual_frames:
        raise AppError(
            "SEQUENCE_REPORT_INVALID",
            "实际视频帧数与完整分段报告不一致",
            {"lengths": lengths, "actual_frames": actual_frames},
        )
    return lengths, report
