"""Deterministic shot intent checks; these do not inspect or certify generated images."""

import json
import re

from app.agents.visual import visual_prose
from app.core.errors import AppError

CONTINUOUS = {"CONTINUE_FRAME", "CONTINUE_VIDEO"}


def reference_roles(bible, shot):
    contract = (shot.get("prompts") or {}).get("visual_continuity")
    characters = {item["id"] for item in (bible or {}).get("characters", [])}
    if contract:
        unknown = set(contract["visible_character_ids"]) - characters
        roles = contract["reference_roles"]
        unknown |= {r[10:] for r in roles if r.startswith("character:")} - characters
        if unknown:
            raise AppError(
                "SHOT_REFERENCE_INVALID",
                "出场或参考角色不在视觉设定中",
                {"characters": sorted(unknown)},
                422,
            )
        props = {item["id"] for item in (bible or {}).get("props", [])}
        unknown_props = {r[5:] for r in roles if r.startswith("prop:")} - props
        if unknown_props:
            raise AppError(
                "SHOT_REFERENCE_INVALID",
                "道具参考不在视觉设定中",
                {"props": sorted(unknown_props)},
                422,
            )
        return roles
    if shot.get("transition_from_previous") in CONTINUOUS:
        return []  # The actual previous frame supplies the image, not an arbitrary character.
    if not characters:
        return ["environment"]
    if len(characters) == 1:
        return [f"character:{next(iter(characters))}"]
    # Exact stable IDs only, never translated names, character order or fuzzy text matching.
    prompt = (shot.get("prompts") or {}).get("start_frame_prompt", "")
    mentioned = [
        id for id in characters if re.search(rf"(?<![\w-]){re.escape(id)}(?![\w-])", prompt)
    ]
    if len(mentioned) == 1:
        return [f"character:{mentioned[0]}"]
    raise AppError(
        "SHOT_REFERENCE_REQUIRED",
        "多角色镜头缺少明确参考选择，请补充 visual_continuity",
        status=422,
    )


def reference_slots(profile):
    bindings = profile.get("bindings", {})
    slots = ["reference_image"] if "reference_image" in bindings else []
    slots += sorted(
        (role for role in bindings if re.fullmatch(r"reference_image_\d+", role)),
        key=lambda role: int(role.rsplit("_", 1)[1]),
    )
    return slots


def continuity_report(episode, image):
    issues, warnings, selections, scenes = [], [], [], {}
    capacity = len(reference_slots(image))
    previous = None
    for shot in episode.get("shots", []):
        if not shot.get("enabled", True) or not shot.get("prompts"):
            continue
        identity = {"shot_id": shot["id"], "shot_index": shot["index"]}
        contract = shot["prompts"].get("visual_continuity")
        inherited = shot.get("transition_from_previous") in CONTINUOUS
        if inherited and previous is None:
            issues.append(
                {
                    **identity,
                    "code": "CONTINUITY_PREVIOUS_REQUIRED",
                    "message": "首个启用镜头不能延续前镜",
                }
            )
        try:
            roles = reference_roles(episode.get("bible"), shot)
        except AppError as exc:
            roles = []
            # A text-only workflow does not bind references, but an explicit invalid ID is still invalid.
            if (capacity and not shot.get("start_frame_asset_id")) or contract:
                issues.append({**identity, **exc.as_dict()})
        selections.append(
            {**identity, "roles": roles, "capacity": capacity, "inherited": inherited}
        )
        if (
            not shot.get("start_frame_asset_id")
            and roles
            and len(roles) + int(inherited)
            < sum(not image["bindings"][r].get("optional", False) for r in reference_slots(image))
        ):
            issues.append(
                {
                    **identity,
                    "code": "SHOT_REFERENCE_INSUFFICIENT",
                    "message": "工作流的参考输入多于本镜选择，请补齐参考素材或使用单参考工作流",
                }
            )
        if (
            "style_reference" in image.get("bindings", {})
            and "style" not in roles
            and not shot.get("start_frame_asset_id")
        ):
            issues.append(
                {
                    **identity,
                    "code": "SHOT_REFERENCE_REQUIRED",
                    "message": "当前工作流需要 style 参考，请在本镜参考列表中明确选择",
                }
            )
        if not contract:
            warnings.append(
                {
                    **identity,
                    "code": "CONTINUITY_LEGACY",
                    "message": "旧镜头未声明空间与动作状态，无法预检跨镜头连续性",
                }
            )
        if roles and len(roles) > capacity and not inherited:
            warnings.append(
                {
                    **identity,
                    "code": "REFERENCE_CAPACITY",
                    "message": f"本镜选择 {len(roles)} 项参考，当前工作流只接收前 {capacity} 项；请复核角色和构图",
                }
            )
        if contract:
            selected_characters = {r[10:] for r in roles if r.startswith("character:")}
            if set(contract["visible_character_ids"]) - selected_characters:
                warnings.append(
                    {
                        **identity,
                        "code": "CHARACTER_REFERENCE_INCOMPLETE",
                        "message": "部分出场角色没有身份参考，请重点复核角色身份与站位",
                    }
                )
            state = scenes.setdefault(contract["scene_id"], {})
            prior_contract = ((previous or {}).get("prompts") or {}).get("visual_continuity")
            if (
                inherited
                and prior_contract
                and (
                    contract["scene_id"] != prior_contract["scene_id"]
                    or contract["framing"] != prior_contract["framing"]
                    or contract["intentional_jump"]
                )
            ):
                issues.append(
                    {
                        **identity,
                        "code": "CONTINUITY_FRAME_CONFLICT",
                        "message": "沿用前镜尾帧时不能同时更换场景、景别或声明跳转；请使用切镜",
                    }
                )
            if contract["intentional_jump"]:
                state.clear()
            for key, value in contract["state_in"].items():
                if key in state and state[key] != value:
                    issues.append(
                        {
                            **identity,
                            "code": "CONTINUITY_STATE_CONFLICT",
                            "message": f"{key} 入镜状态与同场景已知状态不一致",
                            "details": {"key": key, "expected": state[key], "actual": value},
                        }
                    )
            state.update(contract["state_in"])
            state.update(contract["state_out"])
        previous = shot
    return {"valid": not issues, "issues": issues, "warnings": warnings, "selections": selections}


def require_continuity(episode, image):
    report = continuity_report(episode, image)
    if report["issues"]:
        first = report["issues"][0]
        raise AppError(
            first["code"], f"第 {first['shot_index'] + 1} 镜：{first['message']}", report, 422
        )
    return report


def planning_state(shots):
    scenes = {}
    for shot in shots:
        contract = (shot.get("prompts") or {}).get("visual_continuity")
        if not contract or not shot.get("enabled", True):
            continue
        state = scenes.setdefault(contract["scene_id"], {})
        if contract["intentional_jump"]:
            state.clear()
        state.update(contract["state_in"])
        state.update(contract["state_out"])
    return json.dumps(scenes, ensure_ascii=False)


def reference_description(role, description):
    if role.startswith("character:"):
        instruction = "A clean identity reference of this character only. No additional cast. Keep the background simple so its pose or furniture does not dictate future shot composition."
    elif role.startswith("prop:"):
        instruction = "A single object reference on a simple neutral background. Show its exact shape, material, color and distinctive details. No people or extra objects."
    elif role == "environment":
        instruction = "An unoccupied environment reference. No people, animals or character portraits. Show the spatial layout and lighting, not a performed story scene."
    else:
        instruction = "A visual study of lighting, palette and texture in an unoccupied setting. No characters, diagrams, written style guides or captions."
    return (
        instruction
        + " Render the following as visual qualities, never as printed text: "
        + visual_prose(description)
    )


def ordered_reference_prompt(profile, prompt, assets, references, overrides, start_frame=None):
    """Describe the actual ordered inputs after user ownership, never an earlier selection."""
    if not profile.get("capabilities", {}).get("supports_multi_reference") or "prompt" in overrides:
        return prompt
    selected = {**assets, **overrides}
    names = {asset: role for role, asset in references.items()}
    hints = []
    for index, role in enumerate(reference_slots(profile)):
        asset = selected.get(role)
        if not asset:
            continue
        name = (
            "this shot's start state"
            if asset == start_frame
            else names.get(asset, "the supplied visual reference")
        )
        hints.append(
            f"<Picture {index + 1}> supplies {name}; use the requested traits while following the target composition."
        )
    return prompt + ("\n\n" + "\n".join(hints) if hints else "")
