"""Capability identity and scalar ownership independent of node IDs."""

from app.core.errors import AppError
from app.workflows.schema import ParameterOwner, ParameterRule, WorkflowCapability

ASSET_ROLES = {
    "start_frame",
    "end_frame",
    "reference_image",
    "style_reference",
    "reference_video",
    "reference_audio",
}
AI_ROLES = {"prompt", "negative", "camera_motion", "motion_strength"}
SYSTEM_ROLES = {"width", "height", "fps", "batch", "seed"}


def is_asset_role(role: str | None) -> bool:
    return bool(role and (role in ASSET_ROLES or role.startswith("reference_image_")))


def role_owner(role: str | None) -> str:
    if is_asset_role(role):
        return "asset_resolver"
    if role in AI_ROLES:
        return "ai"
    if role == "duration":
        return "director"
    if role in SYSTEM_ROLES:
        return "system"
    return "workflow"


def capability_of(profile: dict) -> WorkflowCapability:
    if profile.get("capability"):
        return WorkflowCapability(profile["capability"])
    media = profile.get("media_type") or profile["type"]
    bindings = profile.get("bindings", {})
    if media == "image":
        return (
            WorkflowCapability.IMAGE_TO_IMAGE
            if any(is_asset_role(r) for r in bindings)
            else WorkflowCapability.TEXT_TO_IMAGE
        )
    return (
        WorkflowCapability.FIRST_LAST_TO_VIDEO
        if profile.get("capabilities", {}).get("supports_end_frame", True)
        else WorkflowCapability.IMAGE_TO_VIDEO
    )


def canonicalize(profile: dict) -> dict:
    capability = capability_of(profile)
    media = profile.get("media_type") or profile.get("type") or capability.media_type
    if media != capability.media_type:
        raise AppError("WORKFLOW_INVALID", "capability 与 media_type 不匹配")
    profile.update(media_type=media, type=media, capability=capability.value)
    caps = dict(profile.get("capabilities", {}))
    caps.update(
        supports_start_frame=media == "video",
        supports_end_frame=capability == WorkflowCapability.FIRST_LAST_TO_VIDEO,
    )
    profile["capabilities"] = caps
    decorate_parameters(profile)
    return profile


def decorate_parameters(profile: dict) -> None:
    rules = profile.get("parameter_rules", {})
    parameters = profile.get("parameters", [])
    unknown = rules.keys() - {item["key"] for item in parameters}
    if unknown:
        raise AppError("WORKFLOW_INVALID", "未知参数规则", {"keys": sorted(unknown)})
    for item in parameters:
        role = next(
            (
                r
                for r, b in profile.get("bindings", {}).items()
                if b["node_id"] == item["node_id"] and b["input"] == item["field"]
            ),
            None,
        )
        owner = "asset_resolver" if item.get("asset_kind") else role_owner(role)
        rule = ParameterRule.model_validate(rules.get(item["key"], {}))
        if rule.owner and rule.owner != owner:
            if (
                role
                or item.get("asset_kind")
                or rule.owner
                not in {ParameterOwner.AI, ParameterOwner.WORKFLOW, ParameterOwner.USER}
            ):
                raise AppError(
                    "WORKFLOW_INVALID",
                    "语义角色的 owner 不可改写；自定义参数可归属 ai/workflow/user",
                    {"key": item["key"], "owner": owner},
                )
            owner = rule.owner.value
        item.update(
            role=role,
            owner=owner,
            editable=rule.editable,
            override_policy=rule.override_policy if rule.editable else "never",
        )


def required_asset_roles(profile: dict) -> set[str]:
    capability = capability_of(profile)
    required = set()
    if capability == WorkflowCapability.IMAGE_TO_IMAGE:
        required.add("reference_image")
    if capability.media_type == "video":
        required.add("start_frame")
    if capability == WorkflowCapability.FIRST_LAST_TO_VIDEO:
        required.add("end_frame")
    return required
