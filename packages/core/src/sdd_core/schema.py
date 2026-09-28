"""JSON Schema exported by the same package that owns the workflow wire format."""

from sdd_core.models import Json


def workflow_schema() -> dict[str, Json]:
    identifier: dict[str, Json] = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"}
    step: dict[str, Json] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "kind"],
        "properties": {
            "id": identifier,
            "kind": {"enum": ["agent", "check", "human", "condition", "operation", "finish"]},
            "handler": {"type": "string"},
            "prompt": {"type": "string"},
            "transitions": {
                "type": "array",
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "prefixItems": [{"type": "string"}, identifier],
                },
            },
            "required": {"type": "boolean"},
            "gate": {"type": "boolean"},
            "mutates": {"type": "boolean"},
            "max_visits": {"type": "integer", "minimum": 1, "maximum": 100},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 86400},
            "profile": {"type": "string"},
            "config": {"type": "string", "contentMediaType": "application/json"},
            "condition_key": {"type": "string"},
            "condition_value": {"type": "string"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SDD workflow v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "entry", "steps"],
        "properties": {
            "id": {"type": "string"},
            "entry": identifier,
            "schema": {"const": 1},
            "max_planning_calls": {"type": ["integer", "null"], "minimum": 0},
            "max_calls": {"type": "integer", "minimum": 1},
            "max_input_chars": {"type": "integer", "minimum": 128},
            "max_tokens": {"type": ["integer", "null"], "minimum": 1},
            "steps": {"type": "array", "minItems": 1, "maxItems": 128, "items": step},
        },
    }
