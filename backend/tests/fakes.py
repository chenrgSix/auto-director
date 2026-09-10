"""Deterministic dependency doubles. This module is never imported by application code."""

import io
import json

import httpx
from PIL import Image

from app.agents.schemas import EpisodePlan, QAResult, ShotPrompts, VisualBible
from app.comfyui.client import ComfyUIClient
from app.core.config import ROOT


class FakeProvider:
    def __init__(self):
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def generate_json(self, system, context, schema, *, images=None):
        self.usage["calls"] += 1
        if issubclass(schema, EpisodePlan):
            count = context["min_shots"]
            data = {
                "title": "Fixture episode",
                "logline": "Synthetic contract test",
                "target_duration": context["target_duration"],
                "shots": [
                    {
                        "index": i,
                        "title": f"Shot {i + 1}",
                        "duration": context["target_duration"] / count,
                        "purpose": "explore",
                        "action": "walk",
                        "camera": "wide",
                        "start_state": "start",
                        "end_state": "end",
                        "transition_from_previous": "CUT" if i == 0 else "CONTINUE_FRAME",
                    }
                    for i in range(count)
                ],
            }
        elif issubclass(schema, VisualBible):
            data = {
                "characters": [
                    {
                        "id": "lion_a",
                        "description": "golden lion",
                        "distinguishing_features": ["dark mane"],
                    }
                ],
                "environment": {"biome": "forest"},
                "style": {"style": "documentary"},
                "continuity_rules": ["One lion"],
                "negative_prompt": "artifacts, text",
                "camera_motion": "static reference view",
                "motion_strength": 0.2,
            }
        elif issubclass(schema, ShotPrompts):
            data = {
                "image_prompt": "A lion",
                "start_frame_prompt": "Lion starts walking",
                "end_frame_prompt": "Lion has walked forward",
                "video_prompt": "Lion walking",
                "negative_prompt": "artifacts",
                "camera_motion": "slow tracking",
                "motion_strength": 0.6,
                "continuity_state": {"direction": "right"},
            }
        elif schema is QAResult:
            assert images and all(path.is_file() for path in images)
            data = {
                "character_consistency": 0.95,
                "scene_consistency": 0.95,
                "style_consistency": 0.95,
                "action_accuracy": 0.95,
                "transition_quality": 0.95,
                "artifact_score": 0.05,
                "explanation": "Synthetic QA fixture, not real visual acceptance",
            }
        else:
            raise AssertionError(schema)
        return schema.model_validate(data)


class FakeComfy:
    def __init__(self, settings, video_bytes):
        self.settings, self.video_bytes = settings, video_bytes
        buffer = io.BytesIO()
        Image.new("RGB", (256, 256), (80, 140, 100)).save(buffer, "PNG")
        self.image_bytes = buffer.getvalue()
        self.prompts = {}
        self.calls = []
        self.info = {}
        for path in (ROOT / "bundled_workflows").rglob("*.json"):
            graph = json.loads(path.read_text())
            for node in graph.values():
                required = {}
                for key, value in node["inputs"].items():
                    kind = (
                        "INT"
                        if type(value) is int
                        else "FLOAT"
                        if type(value) is float
                        else "STRING"
                    )
                    required[key] = [
                        kind,
                        {"image_upload": True} if node["class_type"] == "LoadImage" else {},
                    ]
                self.info[node["class_type"]] = {"input": {"required": required}}

    def client(self, url):
        return ComfyUIClient(
            self.settings, url, transport=httpx.MockTransport(self.handle), use_websocket=False
        )

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "name": "Fixture GPU",
                            "vram_total": 24 * 1024**3,
                            "vram_free": 20 * 1024**3,
                        }
                    ]
                },
            )
        if path == "/object_info":
            return httpx.Response(200, json=self.info)
        if path == "/upload/image":
            return httpx.Response(
                200, json={"name": "fixture.png", "subfolder": "autodirector", "type": "input"}
            )
        if path == "/prompt":
            id = f"prompt-{len(self.prompts)}"
            self.prompts[id] = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": id})
        if path.startswith("/history/"):
            id = path.rsplit("/", 1)[1]
            if id not in self.prompts:
                return httpx.Response(200, json={})
            graph = self.prompts[id]["prompt"]
            video = graph["save"]["class_type"] == "SaveVideo"
            return httpx.Response(
                200,
                json={
                    id: {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "save": {
                                "images": [
                                    {
                                        "filename": "fixture.mp4" if video else "fixture.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                        "prompt": [0, id, graph, {"client_id": self.prompts[id]["client_id"]}],
                    }
                },
            )
        if path == "/view":
            video = request.url.params["filename"].endswith("mp4")
            return httpx.Response(
                200,
                content=self.video_bytes if video else self.image_bytes,
                headers={"Content-Type": "video/mp4" if video else "image/png"},
            )
        if path in {"/queue", "/interrupt", "/history"}:
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": []} if path == "/queue" else {}
            )
        raise AssertionError(path)
