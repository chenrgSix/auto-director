"""Recover per-segment media from an intact Director export using verified frame boundaries."""

from fractions import Fraction

from app.core.errors import AppError
from app.media.service import run_process
from app.workflows.sequence import exported_lengths, sequence_input


async def split_sequence_output(assets, job, records, history, check_cancel):
    if len(records) != 1:
        raise AppError("SEQUENCE_OUTPUT_INVALID", "连续镜头需要一份完整声画输出")
    record = records[0]
    video, audio = record["metadata"].get("video"), record["metadata"].get("audio")
    if not video or not audio:
        raise AppError("SEQUENCE_OUTPUT_INVALID", "连续镜头输出缺少画面或原生音轨")
    try:
        fps = Fraction(video["avg_frame_rate"])
        count = int(video["nb_frames"])
        audio_duration = float(audio["duration"])
        if fps != 24 or count <= 0 or abs(audio_duration - count / 24) > 1 / 24:
            raise ValueError("声画时钟不一致")
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise AppError("SEQUENCE_OUTPUT_INVALID", "连续镜头缺少可靠帧数或声画时长不一致") from exc
    lengths, report = exported_lengths(job["profile_snapshot"], job["input_values"], history, count)
    source = sequence_input(job["input_values"])
    outputs, cursor = [], 0
    for segment, length in zip(source.segments, lengths, strict=True):
        check_cancel()
        target = assets.allocate(job["episode_id"], ".mp4")
        await run_process(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(assets.path(record["id"])),
            "-vf",
            f"trim=start_frame={cursor}:end_frame={cursor + length},setpts=PTS-STARTPTS",
            "-af",
            f"atrim=start={cursor / 24}:end={(cursor + length) / 24},asetpts=PTS-STARTPTS,apad=whole_dur={length / 24}",
            "-t",
            str(length / 24),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target),
        )
        check_cancel()
        output = await assets.register(
            target, job["episode_id"], "SEQUENCE_SEGMENT", segment.shot_id
        )
        if int(output["metadata"]["video"].get("nb_frames", 0)) != length:
            raise AppError("SEQUENCE_OUTPUT_INVALID", "分段后帧数不一致，未绑定任何片段")
        outputs.append(output)
        cursor += length
    return outputs, {
        "report": report,
        "segment_frames": lengths,
        "total_frames": count,
        "source_asset_id": record["id"],
    }
