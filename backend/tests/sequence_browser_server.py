"""Opt-in UI fixture: python -m tests.sequence_browser_server (synthetic AV only)."""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from app.core.config import ROOT, Settings
from app.main import create_app
from app.workflows.schema import WorkflowImport
from tests.sequence_fakes import SequenceComfy, SequenceProvider


def main():
    with tempfile.TemporaryDirectory(prefix="autodirector-sequence-browser-") as temporary:
        config = Settings(
            _env_file=None,
            data_dir=Path(temporary),
            llm_model="SYNTHETIC-NOT-A-REAL-MODEL",
            vlm_model="",
            poll_interval=0.02,
        )
        comfy = SequenceComfy(config, b"")
        app = create_app(config, client_factory=comfy.client, provider_factory=SequenceProvider)
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                request = WorkflowImport.model_validate_json(
                    (
                        ROOT / "workflow_examples/codex_h3_continuous/continuous.profile.json"
                    ).read_text()
                )
                comfy.install(app.state.workflows.import_workflow(request))
                yield

        app.router.lifespan_context = lifespan
        print("SEQUENCE UI FIXTURE: synthetic media, no real model service", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AD_BROWSER_TEST_PORT", "8012")))


if __name__ == "__main__":
    main()
