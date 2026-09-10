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

from app.core.config import Settings
from app.main import create_app
from app.media.service import run_process
from app.workflows.recognition import WorkflowRecognition
from tests.fakes import FakeComfy, FakeProvider


class BrowserProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
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
        app = create_app(config, client_factory=comfy.client, provider_factory=BrowserProvider)
        print("BROWSER ACCEPTANCE FIXTURE: synthetic media, no real model service", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AD_BROWSER_TEST_PORT", "8011")))


if __name__ == "__main__":
    main()
