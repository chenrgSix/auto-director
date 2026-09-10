"""Rebuild the small API-format templates. No models or ComfyUI installation required."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bundled_workflows"


def node(class_type, inputs, title=""):
    return {"class_type": class_type, "inputs": inputs, "_meta": {"title": title or class_type}}


image = {
    "model": node("CheckpointLoaderSimple", {"ckpt_name": "v1-5-pruned-emaonly.safetensors"}),
    "positive": node("CLIPTextEncode", {"text": "cinematic photograph", "clip": ["model", 1]}, "(Input:prompt)"),
    "negative": node("CLIPTextEncode", {"text": "text, watermark, artifacts", "clip": ["model", 1]}, "(Input:negative)"),
    "latent": node("EmptyLatentImage", {"width": 512, "height": 768, "batch_size": 1}, "(Input:width_height) (Input:batch)"),
    "sampler": node("KSampler", {"seed": 42, "steps": 24, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0, "model": ["model", 0], "positive": ["positive", 0], "negative": ["negative", 0], "latent_image": ["latent", 0]}, "(Input:seed)"),
    "decode": node("VAEDecode", {"samples": ["sampler", 0], "vae": ["model", 2]}),
    "save": node("SaveImage", {"filename_prefix": "AutoDirector/image", "images": ["decode", 0]}, "(Output:image)"),
}

video = {
    "model": node("UNETLoader", {"unet_name": "wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors", "weight_dtype": "default"}),
    "sampling": node("ModelSamplingSD3", {"model": ["model", 0], "shift": 8.0}),
    "clip": node("CLIPLoader", {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}),
    "vae": node("VAELoader", {"vae_name": "wan_2.1_vae.safetensors"}),
    "vision": node("CLIPVisionLoader", {"clip_name": "clip_vision_h.safetensors"}),
    "positive": node("CLIPTextEncode", {"text": "natural cinematic motion", "clip": ["clip", 0]}, "(Input:prompt)"),
    "negative": node("CLIPTextEncode", {"text": "artifacts, text, static image", "clip": ["clip", 0]}, "(Input:negative)"),
    "start": node("LoadImage", {"image": "start.png"}, "(Input:start_frame)"),
    "end": node("LoadImage", {"image": "end.png"}, "(Input:end_frame)"),
    "start_vision": node("CLIPVisionEncode", {"clip_vision": ["vision", 0], "image": ["start", 0], "crop": "center"}),
    "end_vision": node("CLIPVisionEncode", {"clip_vision": ["vision", 0], "image": ["end", 0], "crop": "center"}),
    "frames": node("WanFirstLastFrameToVideo", {"positive": ["positive", 0], "negative": ["negative", 0], "vae": ["vae", 0], "width": 720, "height": 1280, "length": 81, "batch_size": 1, "start_image": ["start", 0], "end_image": ["end", 0], "clip_vision_start_image": ["start_vision", 0], "clip_vision_end_image": ["end_vision", 0]}, "(Input:width_height) (Input:duration) (Input:batch)"),
    "sampler": node("KSampler", {"seed": 42, "steps": 30, "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0, "model": ["sampling", 0], "positive": ["frames", 0], "negative": ["frames", 1], "latent_image": ["frames", 2]}, "(Input:seed)"),
    "decode": node("VAEDecode", {"samples": ["sampler", 0], "vae": ["vae", 0]}),
    "video": node("CreateVideo", {"images": ["decode", 0], "fps": 16.0}, "(Input:fps)"),
    "save": node("SaveVideo", {"video": ["video", 0], "filename_prefix": "AutoDirector/video", "format": {"format": "mp4", "codec": {"codec": "h264"}}}, "(Output:video)"),
}

for path, graph in [("text_to_image/default_image.json", image), ("first_last_video/default_video.json", video)]:
    (ROOT / path).write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n")
