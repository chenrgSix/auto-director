"""Conservative local duplicate detection; this does not replace semantic visual QA."""

import asyncio
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat


def compare_keyframes(start: Path, end: Path) -> dict:
    def thumbnail(path):
        with Image.open(path) as source:
            return ImageOps.exif_transpose(source).convert("RGB").resize((96, 96))

    first, last = thumbnail(start), thumbnail(end)
    difference = ImageStat.Stat(ImageChops.difference(first, last))
    mae = sum(difference.mean) / (3 * 255)
    a, b = [
        list(frame.convert("L").filter(ImageFilter.GaussianBlur(1)).tobytes())
        for frame in (first, last)
    ]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    variance = sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b)
    correlation = covariance / math.sqrt(variance) if variance else 0.0
    return {
        "near_duplicate": mae <= 1 / 255 or (correlation >= 0.995 and mae <= 0.04),
        "correlation": round(correlation, 6),
        "normalized_mae": round(mae, 6),
    }


async def inspect_keyframes(start: Path, end: Path) -> dict:
    return await asyncio.to_thread(compare_keyframes, start, end)
