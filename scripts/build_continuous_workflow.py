"""Build the application adapter from the user's single Director canvas, stripping story state."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "workflow_examples/codex_h3_continuous"
FIELDS = {
    "UNETLoader": ["unet_name", "weight_dtype"],
    "CLIPLoader": ["clip_name", "type", "device"],
    "VAELoader": ["vae_name"],
    "ModelAttentionBackend": ["attention"],
    "LoraLoaderModelOnly": ["lora_name", "strength_model"],
    "MiniMaxH3Director": [
        "task_type",
        "global_prompt",
        "bd_grp_sample",
        "cfg",
        "seed",
        None,
        "frame_rate",
        "width",
        "height",
        "ref_max_size",
        "total_frames",
        "timeline_data",
        "bd_grp_advanced",
        "steps",
        "sampler",
        "scheduler",
        "shift_video",
        "shift_audio",
        "bd_grp_perf",
        "clear_vram_between_segments",
        "export_source_images",
    ],
}


def build():
    canvas = json.loads((FOLDER / "codex-H3-Ref2VA-连续镜头.json").read_text())
    links = {edge[0]: edge for edge in canvas["links"]}
    graph = {}
    for node in canvas["nodes"]:
        kind, widgets = node["type"], node["widgets_values"]
        if kind == "MarkdownNote":
            continue
        inputs = (
            dict(widgets)
            if isinstance(widgets, dict)
            else {
                field: value
                for field, value in zip(FIELDS[kind], widgets, strict=True)
                if field
            }
        )
        for entry in node.get("inputs", []):
            if entry.get("link") is not None:
                edge = links[entry["link"]]
                inputs[entry["name"]] = [str(edge[1]), edge[2]]
        graph[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    director = next(
        key for key, node in graph.items() if node["class_type"] == "MiniMaxH3Director"
    )
    video = next(
        key for key, node in graph.items() if node["class_type"] == "VHS_VideoCombine"
    )
    graph[director]["inputs"].update(
        global_prompt="Video segment prompt", timeline_data="{}", total_frames=124
    )
    graph[video]["inputs"]["filename_prefix"] = "autodirector/continuous"
    report = str(max(int(key) for key in graph) + 1)
    graph[report] = {"class_type": "PreviewAny", "inputs": {"source": [director, 5]}}
    fields = {
        "prompt": "global_prompt",
        "sequence": "timeline_data",
        "duration": "total_frames",
        "width": "width",
        "height": "height",
        "fps": "frame_rate",
        "seed": "seed",
    }
    bindings = {
        role: {"node_id": director, "input": field} for role, field in fields.items()
    }
    bindings["duration"].update(
        transform="duration_to_frames", frame_multiple=17, frame_offset=5, frame_fps=24
    )
    profile = {
        "name": "codex-H3-Ref2VA-连续镜头",
        "media_type": "video",
        "capability": "REFERENCE_SEQUENCE_TO_VIDEO",
        "workflow": graph,
        "capabilities": {
            "max_duration": 5,
            "supports_multi_reference": True,
            "supports_start_frame": False,
            "supports_end_frame": False,
            "audio_prompt_format": "minimax_h3",
        },
        "bindings": bindings,
        "outputs": {"video": video, "sequence_report": report},
        "parameter_rules": {
            f"{director}.timeline_data": {"editable": False, "override_policy": "never"}
        },
    }
    for name, data in (
        ("continuous.profile.json", profile),
        ("continuous.api.json", graph),
    ):
        (FOLDER / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )


if __name__ == "__main__":
    build()
