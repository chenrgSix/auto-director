"""Build the optional native Ref2VA profile from the existing H3 sampling chain."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "workflow_examples" / "h3_ref2va"


def build():
    source = ROOT / "workflow_examples/codex_8gb/codex_h3_i2v.profile.json"
    profile = json.loads(source.read_text())
    graph = profile["workflow"]

    def find(kind):
        return next(
            (key, node) for key, node in graph.items() if node["class_type"] == kind
        )

    _, unet = find("UNETLoader")
    unet["inputs"]["unet_name"] = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    unet["_meta"]["title"] = "H3 Ref2VA INT8"
    _, lora = find("LoraLoaderModelOnly")
    lora["inputs"]["lora_name"] = (
        "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
    )
    lora["_meta"]["title"] = "Ref2V Turbo 4-step"
    _, scheduler = find("BasicScheduler")
    scheduler["inputs"]["steps"] = 4
    cond_id, cond = find("MiniMaxH3ImageToVideo")
    old = cond["inputs"]
    first = old["first_frame"]
    cond.update(
        class_type="MiniMaxH3ReferenceToVideo",
        inputs={
            **{key: old[key] for key in ("clip", "vae", "width", "height", "length")},
            "audio_vae": [find("VAEDecodeAudio")[1]["inputs"]["vae"][0], 0],
            "ref_image_size": "match",
            "prompt": "integrated_multimodal_description: [Shot 1] The subject gently moves in the referenced scene.\nSpeech performance: No spoken dialogue.\noverall_soundscape: Quiet room ambience.\nnon_diegetic_music: N/A",
        },
        _meta={"title": "有序人物 / 场景 / 道具参考"},
    )
    # An explicit guide preserves IMAGE_TO_VIDEO's first-frame contract; references
    # remain separate from the starting composition and do not masquerade as keyframes.
    guide_id = "30"
    graph[guide_id] = {
        "class_type": "MiniMaxH3AddGuide",
        "inputs": {
            "positive": [cond_id, 0],
            "latent": [cond_id, 1],
            "vae": old["vae"],
            "image": first,
            "frame_idx": 0,
        },
        "_meta": {"title": "首帧锚定"},
    }
    _, guider = find("BasicGuider")
    guider["inputs"]["conditioning"] = [guide_id, 0]
    for number in range(1, 10):
        ref_id = str(100 + number)
        graph[ref_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": "REQUIRES_REAL_REFERENCE.png"},
            "_meta": {"title": f"参考图 {number}"},
        }
        cond["inputs"][f"ref_images.ref_image_{number - 1}"] = [ref_id, 0]
        role = "reference_image" if number == 1 else f"reference_image_{number}"
        profile["bindings"][role] = {
            "node_id": ref_id,
            "input": "image",
            "optional": number > 1,
        }
    _, output = find("SaveVideo")
    output["inputs"]["filename_prefix"] = "autodirector/H3_Ref2VA"
    profile.update(name="H3-Ref2VA-多图参考与首帧-Turbo4", parameter_rules={})
    profile["capabilities"].update(supports_multi_reference=True, max_duration=5)
    FOLDER.mkdir(exist_ok=True)
    for filename, value in (
        ("h3_ref2va.profile.json", profile),
        ("h3_ref2va.api.json", graph),
    ):
        (FOLDER / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
    return profile


if __name__ == "__main__":
    build()
