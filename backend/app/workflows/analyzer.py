import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any

from app.core.errors import AppError
from app.core.limits import MAX_SHOT_SECONDS
from app.workflows.ownership import (
    canonicalize,
    decorate_parameters,
    is_asset_role,
    required_asset_roles,
)
from app.workflows.schema import Binding, Capabilities

TAG = re.compile(r"\((Input|Output):([a-z_][a-z0-9_]*)\)", re.IGNORECASE)
ALIASES = {
    "prompt": ["text", "prompt", "positive_prompt"],
    "negative": ["text", "negative", "negative_prompt"],
    "seed": ["seed", "noise_seed"],
    "width": ["width"],
    "height": ["height"],
    "start_frame": ["image", "start_image"],
    "end_frame": ["image", "end_image"],
    "reference_image": ["image", "reference_image"],
    "style_reference": ["image", "style_reference"],
    "reference_video": ["file", "video", "reference_video"],
    "reference_audio": ["audio", "file"],
    "duration": ["duration", "length", "frames", "num_frames"],
    "fps": ["fps", "frame_rate"],
    "batch": ["batch_size"],
    "camera_motion": ["camera_motion"],
    "motion_strength": ["motion_strength"],
}


def workflow_hash(graph: dict) -> str:
    return hashlib.sha256(
        json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and type(value[1]) is int
    )


def validate_graph(graph: dict) -> None:
    if not graph or "nodes" in graph or len(graph) > 2000:
        raise AppError("WORKFLOW_INVALID", "请上传 ComfyUI Save (API Format) JSON，最多 2000 节点")
    if len(json.dumps(graph).encode()) > 2 * 1024 * 1024:
        raise AppError("WORKFLOW_INVALID", "Workflow JSON 超过 2 MiB")
    dependencies = {}
    for id, node in graph.items():
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise AppError("WORKFLOW_INVALID", "每个节点需要 class_type 和 inputs", {"node_id": id})
        dependencies[id] = set()
        for field, value in node["inputs"].items():
            if is_link(value):
                if value[0] not in graph or value[1] < 0:
                    raise AppError(
                        "WORKFLOW_INVALID",
                        "节点链接目标不存在或输出索引非法",
                        {"node_id": id, "field": field},
                    )
                dependencies[id].add(value[0])
    remaining = dict(dependencies)
    while remaining:
        ready = {id for id, deps in remaining.items() if not deps}
        if not ready:
            raise AppError("WORKFLOW_INVALID", "工作流存在循环依赖", {"nodes": list(remaining)})
        remaining = {id: deps - ready for id, deps in remaining.items() if id not in ready}


def field_spec(node: dict, field: str, object_info: dict) -> tuple[Any, dict]:
    inputs = object_info.get(node["class_type"], {}).get("input", {})
    definition = {**inputs.get("required", {}), **inputs.get("optional", {})}.get(field, [])
    if not definition:
        return None, {}
    return definition[0], definition[1] if len(definition) > 1 and isinstance(
        definition[1], dict
    ) else {}


def parameter(id: str, node: dict, field: str, value: Any, object_info: dict) -> dict:
    type_info, constraints = field_spec(node, field, object_info)
    choices = type_info if isinstance(type_info, list) else constraints.get("options")
    if (
        constraints.get("image_upload")
        or constraints.get("video_upload")
        or constraints.get("audio_upload")
    ):
        choices = None
    kind = (
        "boolean"
        if type(value) is bool
        else "integer"
        if type(value) is int
        else "number"
        if type(value) is float
        else "text"
    )
    kind = {"INT": "integer", "FLOAT": "number", "BOOLEAN": "boolean", "STRING": "text"}.get(
        str(type_info), kind
    )
    if choices:
        kind = "select"
    elif constraints.get("multiline"):
        kind = "textarea"
    return {
        "key": f"{id}.{field}",
        "node_id": id,
        "field": field,
        "class_type": node["class_type"],
        "type": kind,
        "default": value,
        "min": constraints.get("min"),
        "max": constraints.get("max"),
        "step": constraints.get("step"),
        "enum": choices,
        "role": None,
        "asset_kind": "image"
        if constraints.get("image_upload") or node["class_type"] == "LoadImage"
        else "video"
        if constraints.get("video_upload")
        else "audio"
        if constraints.get("audio_upload")
        else None,
        "owner": "workflow",
        "editable": True,
        "override_policy": "advanced",
    }


def analyze(graph: dict, object_info: dict | None = None) -> dict:
    validate_graph(graph)
    info = object_info or {}
    parameters, bindings, outputs, warnings = [], {}, {}, []
    seen_roles = set()
    seen_outputs = set()
    for id, node in graph.items():
        scalars = {
            key: value
            for key, value in node["inputs"].items()
            if isinstance(value, (str, int, float, bool))
        }
        node_params = [parameter(id, node, key, value, info) for key, value in scalars.items()]
        title = node.get("_meta", {}).get("title", "")
        for direction, tagged_role in TAG.findall(title):
            roles = (
                ["width", "height"]
                if tagged_role.lower() == "width_height"
                else [tagged_role.lower()]
            )
            for role in roles:
                if direction.lower() == "output":
                    if role in seen_outputs:
                        warnings.append(f"输出角色 {role} 重复，请手动选择输出节点")
                        outputs.pop(role, None)
                    else:
                        outputs[role] = id
                        seen_outputs.add(role)
                    continue
                if role in seen_roles:
                    warnings.append(f"输入角色 {role} 重复，请手动绑定")
                    bindings.pop(role, None)
                    continue
                seen_roles.add(role)
                aliases = (
                    ["image", "reference_image"]
                    if role.startswith("reference_image_")
                    else ALIASES.get(role, [role])
                )
                matches = [key for key in aliases if key in scalars]
                if len(matches) != 1:
                    warnings.append(f"节点 {id} 的 {role} 字段不明确，请手动绑定")
                    continue
                field = matches[0]
                transform = (
                    "duration_to_frames"
                    if role == "duration" and field in {"length", "frames", "num_frames"}
                    else "identity"
                )
                bindings[role] = Binding(node_id=id, input=field, transform=transform).model_dump()
        parameters.extend(node_params)
    # Unambiguous strategy/AI scalar names can be filled even without title tags.
    for role in ("width", "height", "fps", "batch", "seed", "camera_motion", "motion_strength"):
        matches = [p for p in parameters if p["field"] in ALIASES[role]]
        if role not in seen_roles and len(matches) == 1:
            item = matches[0]
            bindings[role] = Binding(node_id=item["node_id"], input=item["field"]).model_dump()
    for role, binding in bindings.items():
        for item in parameters:
            if item["key"] == f"{binding['node_id']}.{binding['input']}":
                item["role"] = role
    result = {
        "bindings": bindings,
        "outputs": outputs,
        "parameters": parameters,
        "warnings": warnings,
        "workflow_hash": workflow_hash(graph),
        "required_class_types": sorted({node["class_type"] for node in graph.values()}),
    }
    decorate_parameters(result)
    return result


def validate_bindings(profile: dict) -> list[dict]:
    profile = canonicalize(deepcopy(profile))
    issues = []
    graph = profile["workflow"]
    caps = Capabilities.model_validate(profile["capabilities"])
    if profile["capability"] == "TEXT_TO_IMAGE" and any(
        is_asset_role(role) for role in profile["bindings"]
    ):
        issues.append(
            {"code": "WORKFLOW_INVALID", "message": "带素材输入的图像工作流应声明 IMAGE_TO_IMAGE"}
        )
    if profile["capability"] == "IMAGE_TO_VIDEO" and "end_frame" in profile["bindings"]:
        issues.append(
            {
                "code": "WORKFLOW_INVALID",
                "message": "IMAGE_TO_VIDEO 只消费首帧；带尾帧输入应声明 FIRST_LAST_TO_VIDEO",
            }
        )
    for item in profile["parameters"]:
        if item.get("asset_kind") and not is_asset_role(item.get("role")):
            issues.append(
                {
                    "code": "WORKFLOW_INVALID",
                    "parameter": item["key"],
                    "message": "素材输入必须绑定明确的素材角色",
                }
            )
    occupied = set()
    for role, raw in profile["bindings"].items():
        binding = Binding.model_validate(raw)
        destination = (binding.node_id, binding.input)
        if destination in occupied:
            issues.append(
                {"code": "WORKFLOW_INVALID", "role": role, "message": "同一参数不能绑定多个角色"}
            )
        occupied.add(destination)
        node = graph.get(binding.node_id, {})
        value = node.get("inputs", {}).get(binding.input)
        if binding.input not in node.get("inputs", {}) or is_link(value):
            issues.append(
                {
                    "code": "WORKFLOW_INVALID",
                    "role": role,
                    "node_id": binding.node_id,
                    "field": binding.input,
                    "message": "绑定必须指向已有的可写值，不能覆盖节点链接",
                }
            )
        if binding.transform != "identity" and role != "duration":
            issues.append(
                {"code": "WORKFLOW_INVALID", "role": role, "message": "帧数转换仅用于 duration"}
            )
    required = {"prompt", *required_asset_roles(profile)}
    if profile["media_type"] == "video":
        required.add("duration")
        if caps.supports_video_reference:
            required.add("reference_video")
    for role in required - profile["bindings"].keys():
        issues.append(
            {"code": "WORKFLOW_INVALID", "role": role, "message": f"缺少必须输入角色 {role}"}
        )
    output = profile["media_type"]
    if profile["outputs"].get(output) not in graph:
        issues.append({"code": "WORKFLOW_INVALID", "message": f"缺少 {output} 输出节点"})
    return issues


def check_value(item: dict, value: Any) -> None:
    kind = item["type"]
    valid = True
    if kind == "integer":
        valid = type(value) is int
    elif kind == "number":
        valid = type(value) in {int, float} and math.isfinite(value)
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind in {"text", "textarea"}:
        valid = isinstance(value, str) and len(value) <= 20000
    elif kind == "select":
        valid = type(value) in {str, int, float, bool} and (
            type(value) is not float or math.isfinite(value)
        )
    if not valid:
        raise AppError("WORKFLOW_INVALID", f"参数 {item['key']} 类型不匹配", item)
    if item.get("enum") and not any(
        value == option
        and (
            type(value) is type(option)
            or (type(value) in {int, float} and type(option) in {int, float})
        )
        for option in item["enum"]
    ):
        raise AppError(
            "MISSING_MODEL"
            if any(x in item["field"] for x in ("name", "model"))
            else "WORKFLOW_INVALID",
            f"参数 {item['key']} 的值不在 ComfyUI 可选项中",
            {"value": value, "parameter": item},
        )
    if type(value) in {int, float}:
        for key, violates in [
            ("min", lambda bound: value < bound),
            ("max", lambda bound: value > bound),
        ]:
            if item.get(key) is not None and violates(item[key]):
                raise AppError("WORKFLOW_INVALID", f"参数 {item['key']} 超出 {key} 约束", item)


def validate_ai_parameters(profile: dict, values: dict) -> None:
    parameters = {item["key"]: item for item in profile["parameters"]}
    for key, value in values.items():
        item = parameters.get(key)
        if not item or item["owner"] != "ai":
            raise AppError("LLM_INVALID_OUTPUT", f"AI 不允许填写参数 {key}")
        try:
            check_value(item, value)
        except AppError as exc:
            raise AppError("LLM_INVALID_OUTPUT", exc.message, exc.details) from exc


def patch(
    profile: dict,
    values: dict,
    parameter_values: dict | None = None,
    *,
    advanced: bool = True,
    ai_values: dict | None = None,
) -> dict:
    issues = validate_bindings(profile)
    if issues:
        raise AppError("WORKFLOW_INVALID", "工作流绑定无效", issues)
    graph = deepcopy(profile["workflow"])
    decorated = deepcopy(profile)
    decorate_parameters(decorated)
    parameters = {item["key"]: item for item in decorated["parameters"]}
    validate_ai_parameters(decorated, ai_values or {})
    for key, value in profile.get("parameter_values", {}).items():
        if key not in parameters:
            raise AppError("WORKFLOW_INVALID", f"未知动态参数 {key}")
        item = parameters[key]
        check_value(item, value)
        graph[item["node_id"]]["inputs"][item["field"]] = value
    for key, value in (ai_values or {}).items():
        item = parameters[key]
        graph[item["node_id"]]["inputs"][item["field"]] = value
    for role, value in values.items():
        if role not in profile["bindings"]:
            continue
        binding = Binding.model_validate(profile["bindings"][role])
        if binding.transform == "duration_to_frames":
            fps = values.get("fps", 16)
            if (
                type(fps) not in {int, float}
                or type(value) not in {int, float}
                or not 1 <= fps <= 120
                or not 0 < value <= MAX_SHOT_SECONDS
            ):
                raise AppError("WORKFLOW_INVALID", "duration/fps 超出允许范围")
            value = (
                math.ceil((value * fps - binding.frame_offset) / binding.frame_multiple)
                * binding.frame_multiple
                + binding.frame_offset
            )
        key = f"{binding.node_id}.{binding.input}"
        if key in parameters:
            check_value(parameters[key], value)
        graph[binding.node_id]["inputs"][binding.input] = value
    if parameter_values and not advanced:
        raise AppError("OVERRIDE_NOT_ALLOWED", "参数覆盖需要高级模式")
    for key, value in (parameter_values or {}).items():
        item = parameters.get(key)
        if not item:
            raise AppError("WORKFLOW_INVALID", f"未知动态参数 {key}")
        if not item["editable"] or item["override_policy"] != "advanced":
            raise AppError("OVERRIDE_NOT_ALLOWED", f"参数 {key} 不允许覆盖")
        check_value(item, value)
        graph[item["node_id"]]["inputs"][item["field"]] = value
    return graph


def validate_dependencies(profile: dict, object_info: dict) -> dict:
    issues = validate_bindings(profile)
    graph = patch(profile, {}) if not issues else profile["workflow"]
    for id, node in graph.items():
        info = object_info.get(node["class_type"])
        if info is None:
            issues.append(
                {
                    "code": "MISSING_NODE",
                    "node_id": id,
                    "class_type": node["class_type"],
                    "message": "ComfyUI 未安装此节点",
                }
            )
            continue
        for field in info.get("input", {}).get("required", {}):
            if field not in node["inputs"]:
                issues.append(
                    {
                        "code": "WORKFLOW_INVALID",
                        "node_id": id,
                        "field": field,
                        "message": "缺少节点必填字段",
                    }
                )
        for field, value in node["inputs"].items():
            if is_link(value):
                source = object_info.get(graph[value[0]]["class_type"], {})
                if source.get("output") is not None and value[1] >= len(source["output"]):
                    issues.append(
                        {
                            "code": "WORKFLOW_INVALID",
                            "node_id": id,
                            "field": field,
                            "message": "链接输出索引越界",
                        }
                    )
            elif isinstance(value, (str, int, float, bool)):
                item = parameter(id, node, field, value, object_info)
                # Uploaded input media are filled at execution, not model dependencies.
                if field_spec(node, field, object_info)[1].get("image_upload") or field_spec(
                    node, field, object_info
                )[1].get("video_upload"):
                    continue
                try:
                    check_value(item, value)
                except AppError as exc:
                    issues.append(
                        {
                            **exc.as_dict(),
                            "node_id": id,
                            "class_type": node["class_type"],
                            "field": field,
                        }
                    )
    return {
        "valid": not issues,
        "issues": issues,
        "checked_classes": len({n["class_type"] for n in graph.values()}),
    }
