"""Strict serialization at the boundary; no arbitrary class loading."""

import hashlib
import json
from dataclasses import asdict
from typing import cast

from sdd_core.models import (
    Artifact,
    Attempt,
    Json,
    Kind,
    Result,
    Run,
    Status,
    Step,
    Usage,
    Workflow,
)


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def object_json(raw: str) -> dict[str, Json]:
    value: object = json.loads(raw, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return cast(dict[str, Json], value)


def text(value: Json, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def integer(value: Json, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be integer")
    return value


def flag(value: Json, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


def mapping(value: Json) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError("Expected mapping")
    return value


def sequence(value: Json) -> list[Json]:
    if not isinstance(value, list):
        raise ValueError("Expected array")
    return value


def number(value: Json) -> float:
    if type(value) not in (int, float):
        raise ValueError("Expected number")
    return float(cast(int | float, value))


def workflow_json(workflow: Workflow) -> str:
    return canonical(asdict(workflow))


def workflow_load(raw: str) -> Workflow:
    doc = object_json(raw)
    allowed = {
        "id",
        "entry",
        "steps",
        "schema",
        "max_calls",
        "max_input_chars",
        "max_tokens",
        "max_planning_calls",
    }
    if doc.keys() - allowed:
        raise ValueError("Unknown workflow fields")
    steps: list[Step] = []
    for value in sequence(doc.get("steps")):
        item = mapping(value)
        if item.keys() - Step.__dataclass_fields__.keys():
            raise ValueError("Unknown step fields")
        edges = tuple(
            (text(sequence(pair)[0], "outcome"), text(sequence(pair)[1], "target"))
            for pair in sequence(item.get("transitions", []))
            if len(sequence(pair)) == 2
        )
        if len(edges) != len(sequence(item.get("transitions", []))):
            raise ValueError("Transition must have two elements")
        steps.append(
            Step(
                id=text(item.get("id"), "id"),
                kind=cast(Kind, text(item.get("kind"), "kind")),
                handler=text(item.get("handler", ""), "handler"),
                prompt=text(item.get("prompt", ""), "prompt"),
                transitions=edges,
                required=flag(item.get("required", False), "required"),
                gate=flag(item.get("gate", False), "gate"),
                mutates=flag(item.get("mutates", False), "mutates"),
                max_visits=integer(item.get("max_visits", 3), "max_visits"),
                timeout=integer(item.get("timeout", 900), "timeout"),
                profile=text(item.get("profile", "default"), "profile"),
                config=text(item.get("config", "{}"), "config"),
                condition_key=text(item.get("condition_key", ""), "condition_key"),
                condition_value=text(item.get("condition_value", ""), "condition_value"),
            )
        )
    maximum = doc.get("max_tokens")
    return Workflow(
        text(doc.get("id"), "id"),
        text(doc.get("entry"), "entry"),
        tuple(steps),
        integer(doc.get("schema", 1), "schema"),
        integer(doc.get("max_calls", 10), "max_calls"),
        integer(doc.get("max_input_chars", 24000), "max_input_chars"),
        None if maximum is None else integer(maximum, "max_tokens"),
        None
        if doc.get("max_planning_calls") is None
        else integer(doc["max_planning_calls"], "max_planning_calls"),
    )


def run_json(run: Run) -> str:
    return canonical(asdict(run))


def run_load(raw: str) -> Run:
    d = object_json(raw)
    active = d["active"]
    attempt = None
    if active is not None:
        a = mapping(active)
        attempt = Attempt(
            text(a["id"], "id"),
            text(a["step"], "step"),
            integer(a["generation"], "generation"),
            number(a["started"]),
            number(a["deadline"]),
            text(a["base_revision"], "revision"),
            None if a["previous"] is None else text(a["previous"], "previous"),
        )
    return Run(
        id=text(d["id"], "id"),
        workflow_digest=text(d["workflow_digest"], "digest"),
        step=text(d["step"], "step"),
        revision=text(d["revision"], "revision"),
        status=cast(Status, text(d["status"], "status")),
        paused=flag(d["paused"], "paused"),
        version=integer(d["version"], "version"),
        generation=integer(d["generation"], "generation"),
        active=attempt,
        visits=tuple(
            (text(sequence(x)[0], "step"), integer(sequence(x)[1], "visits"))
            for x in sequence(d["visits"])
        ),
        completed=tuple(text(x, "step") for x in sequence(d["completed"])),
        gates=tuple(
            (text(sequence(x)[0], "step"), text(sequence(x)[1], "revision"))
            for x in sequence(d["gates"])
        ),
        calls=integer(d["calls"], "calls"),
        planning_calls=integer(d.get("planning_calls", 0), "planning_calls"),
        auto_answer=flag(d.get("auto_answer", False), "auto_answer"),
        tokens=integer(d["tokens"], "tokens"),
        usage_unknown=flag(d["usage_unknown"], "usage_unknown"),
        infrastructure_failures=integer(d["infrastructure_failures"], "failures"),
        wake_at=None if d["wake_at"] is None else number(d["wake_at"]),
        reason=text(d["reason"], "reason"),
        previous_attempt=None
        if d["previous_attempt"] is None
        else text(d["previous_attempt"], "previous"),
    )


def result_json(result: Result) -> str:
    return canonical(asdict(result))


def result_load(raw: str) -> Result:
    d = object_json(raw)
    if d.keys() - Result.__dataclass_fields__.keys():
        raise ValueError("Unknown result fields")
    usage = mapping(d.get("usage", {}))

    def optional_int(key: str) -> int | None:
        value = usage.get(key)
        return None if value is None else integer(value, key)

    artifacts = tuple(
        Artifact(
            text(a["path"], "path"), text(a["sha256"], "hash"), text(a["revision"], "revision")
        )
        for a in (mapping(x) for x in sequence(d.get("artifacts", [])))
    )
    return Result(
        text(d.get("attempt_id"), "attempt_id"),
        integer(d.get("generation"), "generation"),
        text(d.get("outcome"), "outcome"),
        text(d.get("reason"), "reason"),
        text(d.get("revision"), "revision"),
        artifacts,
        Usage(
            optional_int("input_tokens"),
            optional_int("output_tokens"),
            optional_int("cache_read"),
            optional_int("cache_write"),
        ),
        None if d.get("standards") is None else flag(d["standards"], "standards"),
        None if d.get("specification") is None else flag(d["specification"], "specification"),
        None if d.get("resume_at") is None else number(d["resume_at"]),
        text(d.get("data", "{}"), "data"),
    )
