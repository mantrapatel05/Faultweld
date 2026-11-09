"""Provider-compatible structured schemas projected from the strict local models."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

from pydantic import JsonValue

from app.agent.diagnosis import ModelDiagnosis
from app.agent.proposal import ModelProposal
from app.domain.schemas import ActionType

SCHEMA_VERSION = "faultweld-output-2"


def _inline_schema(value: object, definitions: dict[str, object]) -> object:
    if isinstance(value, list):
        return [_inline_schema(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        definition_name = reference.rsplit("/", 1)[-1]
        if definition_name not in definitions:
            raise ValueError("unknown schema reference")
        return _inline_schema(deepcopy(definitions[definition_name]), definitions)

    projected: dict[str, object] = {}
    for key in ("type", "enum", "properties", "items"):
        if key in value:
            property_value = value[key]
            if key == "properties" and isinstance(property_value, dict):
                projected[key] = {
                    name: _inline_schema(item, definitions)
                    for name, item in property_value.items()
                }
            else:
                projected[key] = _inline_schema(property_value, definitions)
    properties = projected.get("properties")
    if isinstance(properties, dict):
        projected["required"] = list(properties)
        projected["additionalProperties"] = False
    elif "required" in value:
        projected["required"] = value["required"]
    return projected


def _diagnosis_schema() -> dict[str, object]:
    source = ModelDiagnosis.model_json_schema()
    definitions = cast(dict[str, object], source.get("$defs", {}))
    schema = _inline_schema(source, definitions)
    if not isinstance(schema, dict):
        raise ValueError("diagnosis schema must be an object")
    return cast(dict[str, object], schema)


def _proposal_schema() -> dict[str, object]:
    source = ModelProposal.model_json_schema()
    source_properties = cast(dict[str, object], source["properties"])
    rationale = cast(dict[str, object], source_properties["rationale"])
    evidence_ids = cast(dict[str, object], source_properties["evidence_ids"])
    return {
        "type": "object",
        "properties": {
            "action_type": {
                "type": ["string", "null"],
                "enum": [action.value for action in ActionType] + [None],
            },
            # A fixed nullable slot makes the provider schema closed and strict. The
            # normalizer maps null to {} so local proposal validation retains its contract.
            "parameters": {
                "type": "object",
                "properties": {"replicas": {"type": ["integer", "null"]}},
                "required": ["replicas"],
                "additionalProperties": False,
            },
            "rationale": {"type": "string", **{
                key: value for key, value in rationale.items() if key in {"minLength", "maxLength"}
            }},
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                **{
                    key: value
                    for key, value in evidence_ids.items()
                    if key in {"minItems", "maxItems"}
                },
            },
        },
        "required": ["action_type", "parameters", "rationale", "evidence_ids"],
        "additionalProperties": False,
    }


def schema_for(purpose: str) -> dict[str, object]:
    """Return a strict JSON Schema; local Pydantic validation remains authoritative."""
    if purpose == "diagnose":
        return _diagnosis_schema()
    if purpose == "propose":
        return _proposal_schema()
    raise ValueError("unsupported model purpose")


def normalize_model_output(
    purpose: str, output: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Normalize safe representation differences before strict local validation.

    This is deliberately not a repair-by-guessing layer: required fields, action values,
    parameters, and evidence IDs are never invented or renamed. JSON has one numeric type,
    while the existing model contracts intentionally require floating-point confidence.
    """
    normalized = dict(output)
    if purpose == "diagnose":
        confidence = normalized.get("confidence")
        if isinstance(confidence, int) and not isinstance(confidence, bool):
            normalized["confidence"] = float(confidence)
        hypotheses = normalized.get("hypotheses")
        if isinstance(hypotheses, list):
            normalized_hypotheses: list[JsonValue] = []
            for item in hypotheses:
                if isinstance(item, dict):
                    hypothesis = dict(item)
                    value = hypothesis.get("confidence")
                    if isinstance(value, int) and not isinstance(value, bool):
                        hypothesis["confidence"] = float(value)
                    normalized_hypotheses.append(hypothesis)
                else:
                    normalized_hypotheses.append(item)
            normalized["hypotheses"] = normalized_hypotheses
    if purpose == "propose":
        parameters = normalized.get("parameters")
        if isinstance(parameters, dict) and parameters == {"replicas": None}:
            normalized["parameters"] = {}
    return normalized
