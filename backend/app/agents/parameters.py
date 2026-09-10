"""Workflow-scoped AI output schema; JSON mode still requires local validation."""

from pydantic import Field, create_model, field_validator

from app.core.errors import AppError
from app.workflows.analyzer import validate_ai_parameters


def constrained_output(base, specifications: list[dict]):
    workflows = {}
    for item in specifications:
        if item["owner"] == "ai":
            workflows.setdefault(item["workflow_id"], {})[item["key"]] = item
    properties = {}
    for workflow_id, parameters in workflows.items():
        fields = {}
        for key, item in parameters.items():
            kind = item["type"]
            field = {
                "type": {"text": "string", "textarea": "string", "select": "string"}.get(kind, kind)
            }
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
            fields[key] = field
        properties[workflow_id] = {
            "type": "object",
            "properties": fields,
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
                json_schema_extra={"properties": properties, "additionalProperties": False},
            ),
        ),
    )
