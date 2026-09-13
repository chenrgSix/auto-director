"""Director boundary double with real 24 FPS audio/video; never visual-quality evidence."""

import json
import math
import subprocess

import httpx

from app.agents.schemas import SequenceShotPrompts
from tests.fakes import FakeComfy, FakeProvider


def native_prompt(visual):
    return (
        f"integrated_multimodal_description: [Shot 1] {visual}\n\n"
        "Speech performance: No speech.\noverall_soundscape: Quiet room ambience.\n"
        "non_diegetic_music: None"
    )


class SequenceProvider(FakeProvider):
    async def generate_json(self, system, context, schema, *, images=None):
        if issubclass(schema, SequenceShotPrompts):
            data = (await super().generate_json(system, context, SequenceShotPrompts)).model_dump()
            for field in ("image_prompt", "start_frame_prompt", "end_frame_prompt"):
                data[field] = ""
            data["video_prompt"] = native_prompt("The lion walks continuously.")
            if "action_beats" in schema.model_fields:
                duration = context["shot"]["duration"]
                data["action_beats"] = (
                    [
                        {"start": 0, "end": round(duration / 2, 2), "action": "Continue walking."},
                        {
                            "start": round(duration / 2, 2),
                            "end": duration,
                            "action": "Keep moving forward.",
                        },
                    ]
                    if duration >= 4
                    else []
                )
            return schema.model_validate(data)
        return await super().generate_json(system, context, schema, images=images)


class SequenceComfy(FakeComfy):
    missing_refs = False
    fail_once = False
    malformed_report = False

    def install(self, profile):
        for node in profile["workflow"].values():
            required = {}
            for field, value in node["inputs"].items():
                kind = (
                    "BOOLEAN"
                    if type(value) is bool
                    else "INT"
                    if type(value) is int
                    else "FLOAT"
                    if type(value) is float
                    else "STRING"
                )
                required[field] = [kind, {}]
            self.info[node["class_type"]] = {"input": {"required": required}}

    def handle(self, request):
        path = request.url.path
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            graph = self.prompts.get(pid, {}).get("prompt", {})
            director = next(
                (n for n in graph.values() if n["class_type"] == "MiniMaxH3Director"), None
            )
            if director:
                self.calls.append((request.method, path))
                if self.fail_once:
                    self.fail_once = False
                    raise httpx.ConnectError("Fixture disconnected", request=request)
                timeline = json.loads(director["inputs"]["timeline_data"])
                segments, lines, counts = timeline["segments"], [], []
                for i, segment in enumerate(segments):
                    count = segment["frameCount"]
                    if i:
                        sample = math.ceil((count + 22 - 5) / 17) * 17 + 5
                        count = sample - 22
                        lines.append(
                            f"Seg #{i + 1}: continuity guide — 22f from seg #{i} (AV latent, +audio); sample={sample}f → export {count}f"
                        )
                    counts.append(count)
                    refs = 0 if self.missing_refs else len(segment["refs"])
                    lines.append(
                        f"Segment {i + 1}/{len(segments)}: r2v — Reference-to AV (~{refs} ref image(s), 0 ref video(s))"
                    )
                report = "\n".join(lines) if not self.malformed_report else "unsupported report"
                folder = self.settings.data_dir / "sequence-fixtures"
                folder.mkdir(exist_ok=True)
                target = folder / f"{sum(counts)}.mp4"
                if not target.exists():
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-y",
                            "-f",
                            "lavfi",
                            "-i",
                            "testsrc2=size=256x256:rate=24",
                            "-f",
                            "lavfi",
                            "-i",
                            "sine=frequency=440:sample_rate=32000",
                            "-t",
                            str(sum(counts) / 24),
                            "-c:v",
                            "libx264",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                            str(target),
                        ],
                        check=True,
                    )
                self.sequence_bytes = target.read_bytes()
                video = next(
                    key for key, n in graph.items() if n["class_type"] == "VHS_VideoCombine"
                )
                output = next(key for key, n in graph.items() if n["class_type"] == "PreviewAny")
                return httpx.Response(
                    200,
                    json={
                        pid: {
                            "status": {"completed": True, "status_str": "success"},
                            "outputs": {
                                video: {
                                    "gifs": [
                                        {
                                            "filename": "sequence.mp4",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                },
                                output: {"text": [report]},
                            },
                            "prompt": [
                                0,
                                pid,
                                graph,
                                {"client_id": self.prompts[pid]["client_id"]},
                            ],
                        }
                    },
                )
        if path == "/view" and request.url.params.get("filename") == "sequence.mp4":
            self.calls.append((request.method, path))
            return httpx.Response(
                200, content=self.sequence_bytes, headers={"content-type": "video/mp4"}
            )
        return super().handle(request)
