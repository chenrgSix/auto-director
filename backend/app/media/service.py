import asyncio
import hashlib
import json
import math
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError
from app.core.security import safe_path
from app.db.store import Store, uid

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}


async def run_process(*args: str, timeout: float = 300) -> bytes:  # noqa: ASYNC109
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError(
            "CONFIGURATION_REQUIRED", f"未安装 {args[0]}，请先安装 FFmpeg/ffprobe", status=409
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise AppError(
            "COMPOSE_FAILED",
            f"{args[0]} 执行失败",
            stderr.decode(errors="replace")[-3000:],
            status=502,
        )
    return stdout


async def probe(path: Path) -> dict:
    raw = await run_process(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
        timeout=30,
    )
    try:
        data = json.loads(raw)
        streams = data.get("streams", [])
        return {
            "duration": float(data.get("format", {}).get("duration", 0)),
            "video": next((s for s in streams if s.get("codec_type") == "video"), None),
            "audio": next((s for s in streams if s.get("codec_type") == "audio"), None),
        }
    except (ValueError, TypeError) as exc:
        raise AppError("INVALID_MEDIA", "无法读取媒体元数据") from exc


def inspect_image(path: Path) -> dict:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height > 32_000_000 or min(width, height) < 16:
                raise AppError("INVALID_MEDIA", "图像尺寸超出允许范围")
            kind = image.format
            image.verify()
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
        if expected.get(path.suffix.lower()) != kind:
            raise AppError("INVALID_MEDIA", "图片内容与扩展名不匹配")
        return {"width": width, "height": height, "format": kind}
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AppError("INVALID_MEDIA", "无法解码图片内容") from exc


async def inspect_media(path: Path) -> dict:
    extension = path.suffix.lower()
    if extension in IMAGE_EXT:
        return {"kind": "image", **await asyncio.to_thread(inspect_image, path)}
    if extension not in VIDEO_EXT | AUDIO_EXT:
        raise AppError("INVALID_MEDIA", "仅支持 PNG/JPEG/WebP、MP4/WebM/MKV/MOV 和常见音频")
    data = await probe(path)
    if (
        data["duration"] <= 0
        or (extension in VIDEO_EXT and not data["video"])
        or (extension in AUDIO_EXT and not data["audio"])
    ):
        raise AppError("INVALID_MEDIA", "媒体内容与扩展名不符或时长无效")
    return {"kind": "video" if extension in VIDEO_EXT else "audio", **data}


def video_duration(metadata: dict) -> float:
    """Use the playable video length; a script estimate is never a cutoff."""
    video = metadata.get("video") or {}
    value = video.get("duration")
    try:
        actual = float(metadata.get("duration", 0) if value in {None, "N/A"} else value)
    except (ValueError, TypeError):
        actual = 0
    if not video or not math.isfinite(actual) or actual <= 0:
        raise AppError("INVALID_MEDIA", "视频缺少有效画面或时长")
    return actual


def media_duration(metadata: dict) -> float:
    duration = video_duration(metadata)
    audio = metadata.get("audio") or {}
    if audio.get("duration") not in {None, "N/A"}:
        duration = max(duration, float(audio["duration"]))
    elif audio:
        duration = max(duration, float(metadata["duration"]))
    if not math.isfinite(duration):
        raise AppError("INVALID_MEDIA", "音视频时长无效")
    return duration


async def extract_frame(source: Path, target: Path, fraction: float = 1) -> Path:
    metadata = await probe(source)
    duration = video_duration(metadata)
    video = metadata["video"] or {}
    if video.get("duration") not in {None, "N/A"}:
        duration = min(duration, float(video["duration"]))
    try:
        frame_interval = 1 / float(Fraction(video["avg_frame_rate"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        frame_interval = 0.08
    timestamp = max(0, duration * fraction - (frame_interval if fraction == 1 else 0))
    await run_process(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-ss",
        str(timestamp),
        "-frames:v",
        "1",
        str(target),
        timeout=60,
    )
    await asyncio.to_thread(inspect_image, target)
    return target


async def compose(clips: list[Path], output: Path, width: int, height: int, fps: int) -> dict:
    if not clips:
        raise AppError("COMPOSE_FAILED", "没有可合成的已启用镜头")
    with tempfile.TemporaryDirectory(prefix="compose-", dir=output.parent) as temporary:
        root = Path(temporary)
        normalized = []
        source_durations = []
        for index, path in enumerate(clips):
            metadata = await probe(path)
            duration = media_duration(metadata)
            source_durations.append(duration)
            visual_duration = video_duration(metadata)
            # Preserve longer audio with a held final image. Sub-frame codec
            # rounding does not justify adding a whole extra video frame.
            audio_tail = duration - visual_duration
            hold = (
                f",tpad=stop_mode=clone:stop_duration={audio_tail}" if audio_tail > 1 / fps else ""
            )
            audio = metadata.get("audio") or {}
            # AAC decoders can expose padded samples past the source stream's
            # declared end. Remove only codec padding, never story content.
            audio_end = audio.get("duration")
            decoded_audio = (
                f",atrim=end_sample={round(float(audio_end) * 48000)}"
                if audio_end not in {None, "N/A"}
                else ""
            )
            target = root / f"clip-{index:03d}.mov"
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
            ]
            if not metadata["audio"]:
                args += [
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r=48000:cl=stereo:d={visual_duration}",
                ]
            args += [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if metadata["audio"] else "1:a:0",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS{hold},fps={fps}:round=up:eof_action=pass",
                "-af",
                f"aresample=48000,asetpts=PTS-STARTPTS{decoded_audio},apad=whole_dur={math.ceil(visual_duration * fps - 0.001) / fps}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(target),
            ]
            await run_process(*args, timeout=600)
            normalized.append((target, media_duration(await probe(target))))
        manifest = root / "concat.txt"
        await asyncio.to_thread(
            manifest.write_text,
            "\n".join(
                f"file '{path.name}'\nduration {duration:.9f}" for path, duration in normalized
            ),
        )
        await run_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(manifest),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
            timeout=600,
        )
    final = await probe(output)
    expected = sum(duration for _, duration in normalized)
    if not final["video"] or abs(final["duration"] - expected) > max(0.1, 1 / fps):
        raise AppError("COMPOSE_FAILED", "成片时长与完整片段总时长不符", final)
    return {**final, "source_durations": source_durations}


class Assets:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.root / "assets"
        self.root.mkdir(parents=True, exist_ok=True)

    def allocate(self, episode_id: str, extension: str) -> Path:
        path = safe_path(self.root, f"{episode_id}/{uid()}{extension}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, asset_id: str) -> Path:
        record = self.store.get("asset", asset_id)
        path = safe_path(self.root, record["path"])
        if not path.is_file():
            raise AppError("OUTPUT_NOT_FOUND", "资产文件已丢失", {"asset_id": asset_id}, status=404)
        return path

    async def register(
        self, path: Path, episode_id: str, type: str, shot_id: str | None = None
    ) -> dict:
        metadata = await inspect_media(path)

        def fingerprint():
            with path.open("rb") as file:
                return hashlib.file_digest(file, "sha256").hexdigest()

        return self.store.create(
            "asset",
            {
                "episode_id": episode_id,
                "shot_id": shot_id,
                "type": type,
                "path": str(path.relative_to(self.root)),
                "metadata": metadata,
                "size": (await asyncio.to_thread(path.stat)).st_size,
                "sha256": await asyncio.to_thread(fingerprint),
            },
            parent=episode_id,
        )

    async def delete_episode(self, episode_id: str) -> None:
        folder = safe_path(self.root, episode_id)
        if await asyncio.to_thread(folder.exists):
            await asyncio.to_thread(shutil.rmtree, folder)
        for record in self.store.list("asset", episode_id):
            self.store.delete("asset", record["id"])
