"""Omit unused optional reference loaders without ever submitting placeholder files."""

import re
from copy import deepcopy

from app.core.errors import AppError


def optional_targets(profile, info=None):
    graph = profile["workflow"]
    targets = {}
    for role, binding in profile["bindings"].items():
        if not binding.get("optional"):
            continue
        source = binding["node_id"]
        node = graph.get(source, {})
        uses = [
            (id, field)
            for id, target in graph.items()
            for field, value in target["inputs"].items()
            if isinstance(value, list) and value and value[0] == source
        ]
        valid = (
            re.fullmatch(r"reference_image_[2-9]", role)
            and node.get("class_type") == "LoadImage"
            and binding["input"] == "image"
            and len(uses) == 1
            and source not in profile["outputs"].values()
        )
        if valid and info is not None:
            target, field = uses[0]
            definition = (
                info.get(graph[target]["class_type"], {})
                .get("input", {})
                .get("optional", {})
                .get(field)
            )
            valid = bool(definition and definition[0] == "IMAGE")
        if not valid:
            raise AppError(
                "WORKFLOW_INVALID",
                "可选参考必须使用独立 LoadImage 连接一个可选 IMAGE 输入",
                {"role": role},
                422,
            )
        targets[role] = (source, *uses[0])
    return targets


def select_optional_references(profile, bindings, info):
    targets = optional_targets(profile, info)
    selected = deepcopy(profile)
    # Numbering must be contiguous, otherwise Picture N would silently change meaning.
    slots = sorted(
        ["reference_image"] + list(targets),
        key=lambda role: 1 if role == "reference_image" else int(role.rsplit("_", 1)[1]),
    )
    missing = False
    for role in slots:
        if role not in selected["bindings"]:
            continue
        if not bindings.get(role):
            missing = True
        elif missing and targets:
            raise AppError(
                "ASSET_REQUIRED", "多图参考必须从第一张连续选择，不能跳过中间输入", status=422
            )
    for role, (source, target, field) in targets.items():
        if bindings.get(role):
            continue
        del selected["workflow"][target]["inputs"][field]
        del selected["workflow"][source]
        del selected["bindings"][role]
        selected["parameters"] = [p for p in selected["parameters"] if p["node_id"] != source]
        for key in ("parameter_values", "parameter_rules"):
            selected[key] = {
                k: v for k, v in selected.get(key, {}).items() if not k.startswith(source + ".")
            }
    return selected
