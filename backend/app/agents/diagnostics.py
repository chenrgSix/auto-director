"""Small saved-configuration checks using the same JSON/vision provider as generation."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal

from PIL import Image
from pydantic import ValidationError

from app.agents.provider import MODEL_TIMEOUT_SECONDS
from app.agents.schemas import StrictModel
from app.core.errors import AppError

TEST_TIMEOUT_SECONDS = MODEL_TIMEOUT_SECONDS


class ModelTestRequest(StrictModel):
    kind: Literal["director", "vision"] = "director"


class DirectorProbe(StrictModel):
    result: Literal["ok"]


class VisionProbe(StrictModel):
    color: Literal["red", "green", "blue"]


async def check_model(config, provider, kind):
    started = monotonic()
    model = config.vlm_model if kind == "vision" else config.llm_model
    error = None
    try:
        if not model:
            raise AppError(
                "CONFIGURATION_REQUIRED",
                "请先保存视觉模型名称" if kind == "vision" else "请先保存导演模型名称",
            )
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            if kind == "vision":
                with TemporaryDirectory(prefix="autodirector-model-test-") as temporary:
                    path = Path(temporary) / "color.png"
                    Image.new("RGB", (32, 32), (0, 0, 255)).save(path)
                    result = await provider.generate_json(
                        "Inspect the image. Return its dominant color as red, green, or blue in JSON.",
                        {},
                        VisionProbe,
                        images=[path],
                    )
                    result = VisionProbe.model_validate_json(result.model_dump_json())
                    if result.color != "blue":
                        raise AppError(
                            "LLM_VISION_TEST_FAILED",
                            "已收到 JSON，但测试图识别不正确，请检查视觉模型能力",
                        )
            else:
                result = await provider.generate_json(
                    'Return exactly {"result":"ok"} as JSON. No explanation is needed.',
                    {},
                    DirectorProbe,
                )
                DirectorProbe.model_validate_json(result.model_dump_json())
    except TimeoutError:
        error = AppError(
            "LLM_TIMEOUT",
            f"模型测试超过 {TEST_TIMEOUT_SECONDS} 秒，已停止等待",
            {"phase": "test_deadline"},
        )
    except ValidationError:
        error = AppError("LLM_INVALID_OUTPUT", "模型未返回符合测试要求的 JSON")
    except AppError as exc:
        error = exc
    return {
        "kind": kind,
        "model": model,
        "success": error is None,
        "elapsed_seconds": round(monotonic() - started, 3),
        "timeout_seconds": TEST_TIMEOUT_SECONDS,
        "checks": (["json_output", "image_input"] if kind == "vision" else ["json_output"])
        if error is None
        else [],
        "error": error.as_dict() if error else None,
    }
