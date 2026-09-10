"""Conservative suggestions from writable fields and actual graph connections."""

from app.workflows.schema import Binding

SAVE_NODES = {"SaveImage": "image", "SaveVideo": "video", "VHS_VideoCombine": "video"}
FRAME_NODES = {"WanFirstLastFrameToVideo", "WanImageToVideo"}
FIELD_ROLES = {
    "width": ("width",),
    "height": ("height",),
    "fps": ("fps", "frame_rate"),
    "batch": ("batch_size",),
    "seed": ("seed", "noise_seed"),
    "camera_motion": ("camera_motion",),
    "motion_strength": ("motion_strength",),
}


def link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and type(value[1]) is int
    )


def discover(graph, parameters, bindings, outputs, tagged_roles, tagged_outputs, capability=None):
    inputs, results = {}, {}
    by_node = {}
    for item in parameters:
        by_node.setdefault(item["node_id"], []).append(item)

    def candidate(role, item, reason, *, automatic=True, binding=None):
        entry = {
            "key": item["key"],
            "node_id": item["node_id"],
            "label": graph[item["node_id"]].get("_meta", {}).get("title") or item["class_type"],
            "field": item["field"],
            "reason": reason,
            "automatic": automatic,
            "binding": binding
            or Binding(node_id=item["node_id"], input=item["field"]).model_dump(),
        }
        inputs.setdefault(role, {})[item["key"]] = entry

    def upstream(node_id, role, visited=None):
        visited = set() if visited is None else visited
        if node_id in visited:
            return []
        visited.add(node_id)
        node = graph[node_id]
        if role in {"prompt", "negative"}:
            found = [
                p
                for p in by_node.get(node_id, [])
                if (
                    node["class_type"].startswith("CLIPTextEncode")
                    and p["field"] in {"text", "text_g", "text_l"}
                    and not p.get("asset_kind")
                )
            ]
            fields = {
                "positive" if role == "prompt" else "negative",
                "conditioning",
                "conditioning_1",
                "conditioning_2",
            }
        else:
            found = [p for p in by_node.get(node_id, []) if p.get("asset_kind") == "image"]
            fields = {"image", "images", "pixels"}
        if found:
            return found
        return [
            p
            for field, value in node["inputs"].items()
            if field in fields and link(value)
            for p in upstream(value[0], role, visited)
        ]

    for node_id, node in graph.items():
        media = SAVE_NODES.get(node["class_type"])
        if media and node["inputs"].get("save_output", True) is not False:
            results.setdefault(media, []).append(
                {
                    "node_id": node_id,
                    "label": node.get("_meta", {}).get("title") or node["class_type"],
                    "reason": "保存生成结果的节点",
                }
            )
        for field, role in (
            ("positive", "prompt"),
            ("negative", "negative"),
            ("start_image", "start_frame"),
            ("end_image", "end_frame"),
        ):
            value = node["inputs"].get(field)
            if link(value):
                for item in upstream(value[0], role):
                    candidate(role, item, f"连接到 {node['class_type']} 的 {field} 输入")
        if node["class_type"] == "VAEEncode" and link(node["inputs"].get("pixels")):
            for item in upstream(node["inputs"]["pixels"][0], "reference_image"):
                candidate("reference_image", item, "参考图连接到图像编码器")
        for item in by_node.get(node_id, []):
            field = item["field"]
            if not item.get("asset_kind") and item["type"] in {"text", "textarea"}:
                if field in {"prompt", "positive_prompt", "negative_prompt", "negative"}:
                    candidate(
                        "negative" if "negative" in field else "prompt", item, "明确的提示词字段"
                    )
            if field in {"duration", "length", "frames", "num_frames"} and item["type"] in {
                "integer",
                "number",
            }:
                seconds = field == "duration"
                known = node["class_type"] in FRAME_NODES
                candidate(
                    "duration",
                    item,
                    "时长单位为秒"
                    if seconds
                    else ("已识别模型帧数规则 4n+1" if known else "需要在高级设置确认模型帧数规则"),
                    automatic=seconds or known,
                    binding=Binding(
                        node_id=node_id,
                        input=field,
                        transform="identity" if seconds else "duration_to_frames",
                    ).model_dump(),
                )
            for role, names in FIELD_ROLES.items():
                if field in names and not item.get("asset_kind"):
                    expected = (
                        {"text", "textarea"} if role == "camera_motion" else {"integer", "number"}
                    )
                    if item["type"] in expected:
                        candidate(role, item, "唯一且类型匹配的参数字段")

    # Tags remain authoritative, including duplicates that explicitly require confirmation.
    for role, binding in bindings.items():
        item = next(
            (p for p in parameters if p["key"] == f"{binding['node_id']}.{binding['input']}"), None
        )
        if item:
            candidate(role, item, "工作流已标注用途", binding=binding)
    for role in tagged_roles:
        for node_id, node in graph.items():
            if f"(input:{role})" in node.get("_meta", {}).get("title", "").lower():
                for item in by_node.get(node_id, []):
                    if item["field"] in {"text", "prompt", "image", role}:
                        candidate(role, item, "工作流标注存在多个候选", automatic=False)
    for media, node_id in outputs.items():
        if not any(c["node_id"] == node_id for c in results.get(media, [])):
            results.setdefault(media, []).append(
                {
                    "node_id": node_id,
                    "label": graph[node_id]["class_type"],
                    "reason": "工作流已标注输出",
                }
            )

    if capability:
        video = capability.endswith("TO_VIDEO")
    else:
        video = bool(results.get("video") or "video" in outputs)
    suggested = None
    if video:
        suggested = "FIRST_LAST_TO_VIDEO" if inputs.get("end_frame") else "IMAGE_TO_VIDEO"
        inputs.pop("reference_image", None)
    elif results.get("image") or "image" in outputs:
        suggested = (
            "IMAGE_TO_IMAGE"
            if inputs.get("reference_image") or "reference_image" in bindings
            else "TEXT_TO_IMAGE"
        )
    selected = capability or suggested
    if selected == "IMAGE_TO_VIDEO":
        inputs.pop("end_frame", None)
    if selected in {"TEXT_TO_IMAGE", "IMAGE_TO_IMAGE"}:
        for role in ("start_frame", "end_frame", "duration"):
            inputs.pop(role, None)

    occupied = {(b["node_id"], b["input"]) for b in bindings.values()}
    for role, choices in inputs.items():
        if role in bindings or role in tagged_roles or len(choices) != 1:
            continue
        entry = next(iter(choices.values()))
        binding = entry["binding"]
        target = (binding["node_id"], binding["input"])
        if entry["automatic"] and target not in occupied:
            bindings[role] = binding
            occupied.add(target)
    for media, choices in results.items():
        if media not in outputs and media not in tagged_outputs and len(choices) == 1:
            outputs[media] = choices[0]["node_id"]
    return {
        "suggested_capability": suggested,
        "inputs": {role: list(choices.values()) for role, choices in inputs.items()},
        "outputs": results,
    }
