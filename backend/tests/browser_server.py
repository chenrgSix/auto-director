"""Explicit browser-test server. No ComfyUI/LLM network calls; media is synthetic.

Run only for UI acceptance: python -m tests.browser_server
This server is never the production entrypoint.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import uvicorn

from app.agents.diagnostics import DirectorProbe, VisionProbe
from app.agents.script_review import ScriptReview
from app.agents.shot_batch import ShotPromptBatch
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.media.service import run_process
from app.workflows.recognition import WorkflowRecognition
from tests.fakes import FakeComfy, FakeProvider


class BrowserProvider(FakeProvider):
    def __init__(self, config):
        super().__init__()
        self.config = config

    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, ShotPromptBatch) and context["idea"].startswith("C49_BATCH_FIXTURE"):
            # Long enough to inspect persisted partial results and cancel without real models.
            await asyncio.sleep(2 if context["shots"][0]["index"] == 0 else 45)
        if issubclass(schema, ScriptReview) and context["idea"].startswith("C46_REVIEW_FIXTURE"):
            return schema.model_validate(
                {
                    "summary": "合成测试：修正首镜起点，另有一项动作负担问题供用户检查。",
                    "fixes": [
                        {
                            "shot_index": 0,
                            "reason": "合成测试：起点与行走动作衔接。",
                            "changes": {
                                "start_state": "Lion stands at the left edge before walking."
                            },
                        }
                    ],
                    "issues": [
                        {
                            "shot_index": 0,
                            "message": "合成测试：请核对短镜中的动作是否过多。",
                            "suggestion": "查看此镜的视频提示词，简化无关动作后再确认。",
                        }
                    ],
                }
            )
        if schema in (DirectorProbe, VisionProbe):
            # Explicit diagnostic fixtures, selected through the settings editor.
            model = self.config.vlm_model if images else self.config.llm_model
            if model == "TEST-SLOW":
                await asyncio.Event().wait()
            if model == "TEST-AUTH-FAIL":
                raise AppError("LLM_AUTH_FAILED", "模型服务鉴权失败（HTTP 401）")
            return schema.model_validate({"color": "blue"} if images else {"result": "ok"})
        if issubclass(schema, WorkflowRecognition):
            # Fixed UI-only response, never used by the real application's provider.
            if "slow_fixture" in context["workflow"]:
                await asyncio.Event().wait()
            return schema.model_validate_json(
                json.dumps(
                    {
                        "capability": "TEXT_TO_IMAGE",
                        "bindings": {
                            "prompt": {
                                "node_id": "custom",
                                "input": "words",
                                "reason": "浏览器测试夹具：画面描述",
                            }
                        },
                        "outputs": {"image": {"node_id": "save", "reason": "测试最终输出"}},
                        "notes": ["固定测试响应，不代表真实模型识别效果"],
                    }
                )
            )
        return await super().generate_json(system, context, schema, images=images)


def main():
    with tempfile.TemporaryDirectory(prefix="autodirector-browser-test-") as temporary:
        root = Path(temporary)
        clip = root / "synthetic.mp4"
        asyncio.run(
            run_process(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=256x256:rate=16",
                "-t",
                "5.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            )
        )
        config = Settings(
            _env_file=None,
            data_dir=root / "data",
            llm_model="TEST-FIXTURE-NOT-A-REAL-MODEL",
            vlm_model="TEST-FIXTURE-VISION",
            poll_interval=0.02,
        )
        comfy = FakeComfy(config, clip.read_bytes())
        app = create_app(
            config,
            client_factory=comfy.client,
            provider_factory=lambda: BrowserProvider(app.state.config),
        )
        print("BROWSER ACCEPTANCE FIXTURE: synthetic media, no real model service", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AD_BROWSER_TEST_PORT", "8011")))


if __name__ == "__main__":
    main()
