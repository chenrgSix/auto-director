"""Workflow-scoped AI output schema; JSON mode still requires local validation."""

from pydantic import Field, create_model, field_validator

from app.core.errors import AppError
from app.workflows.analyzer import validate_ai_parameters


def parameter_schema(item: dict) -> dict:
    kind = item["type"]
    field = {"type": {"text": "string", "textarea": "string", "select": "string"}.get(kind, kind)}
    if kind in {"text", "textarea"}:
        field["maxLength"] = 20000
    for source, target in (("min", "minimum"), ("max", "maximum"), ("enum", "enum")):
        if item.get(source) is not None:
            field[target] = item[source]
    if kind == "select" and item.get("enum"):
        field["type"] = sorted(
            {
                {str: "string", int: "integer", float: "number", bool: "boolean"}[type(v)]
                for v in item["enum"]
            }
        )
    step = item.get("step")
    if type(step) in {int, float} and step > 0:
        origin = item.get("min") or 0
        # JSON Schema multipleOf is zero-based; ComfyUI step starts at min.
        # A shifted sequence stays explicit in the prompt and is enforced locally.
        if origin == 0:
            field["multipleOf"] = step
        field["description"] = (
            f"For {item['key']}, the value must equal {origin} + n * {step} "
            "for an integer n, within the stated bounds and enum. "
            "Every downstream constraint must also hold for this same value."
        )
    consumers = item.get("downstream_constraints") or []
    if consumers:
        field["allOf"] = [parameter_schema(consumer) for consumer in consumers]
    return field


def constrained_output(base, specifications: list[dict]):
    workflows = {}
    for item in specifications:
        if item["owner"] == "ai" and item.get("role") != "prompt":
            workflows.setdefault(item["workflow_id"], {})[item["key"]] = item
    properties = {}
    for workflow_id, parameters in workflows.items():
        properties[workflow_id] = {
            "type": "object",
            "properties": {key: parameter_schema(item) for key, item in parameters.items()},
            "additionalProperties": False,
        }

    @field_validator("ai_parameters")
    @classmethod
    def validate(cls, mapping):
        for workflow_id, values in mapping.items():
            if workflow_id not in workflows:
                raise ValueError(f"AI 不允许填写工作流 {workflow_id}")
            try:
                validate_ai_parameters(
                    {"parameters": list(workflows[workflow_id].values())}, values
                )
            except AppError as exc:
                raise ValueError(exc.message) from exc
        return mapping

    return create_model(
        f"Constrained{base.__name__}",
        __base__=base,
        __validators__={"validate_ai_parameters": validate},
        ai_parameters=(
            dict[str, dict[str, object]],
            Field(
                default_factory=dict,
                description=(
                    "Additional AI-owned workflow inputs only. The prompt role is supplied "
                    "automatically from the current stage's visual prompt; never duplicate it "
                    "here or use it for narration. Respect every source and downstream constraint, "
                    "including each step sequence and its own origin."
                ),
                json_schema_extra={"properties": properties, "additionalProperties": False},
            ),
        ),
    )
