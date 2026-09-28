"""Validate executable semantics independently of editor layout."""

import re

from sdd_core.codec import object_json
from sdd_core.models import Workflow


def validate(workflow: Workflow, mandatory: tuple[str, ...] = ()) -> None:
    if workflow.schema != 1 or not 1 <= len(workflow.steps) <= 128:
        raise ValueError("Unsupported schema or step count")
    if workflow.max_calls < 1 or workflow.max_input_chars < 128:
        raise ValueError("Invalid budgets")
    if workflow.max_tokens is not None and workflow.max_tokens < 1:
        raise ValueError("Invalid token budget")
    if workflow.max_planning_calls is not None and workflow.max_planning_calls < 0:
        raise ValueError("Invalid planning call budget")
    steps = {s.id: s for s in workflow.steps}
    if len(steps) != len(workflow.steps) or workflow.entry not in steps:
        raise ValueError("Duplicate step or missing entry")
    finishes = {s.id for s in workflow.steps if s.kind == "finish"}
    if not finishes:
        raise ValueError("No finish")
    for step in workflow.steps:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", step.id):
            raise ValueError("Invalid step id")
        if step.kind not in ("agent", "check", "human", "condition", "operation", "finish"):
            raise ValueError("Unknown kind")
        if not 1 <= step.max_visits <= 100 or not 1 <= step.timeout <= 86400:
            raise ValueError("Unbounded step")
        config = object_json(step.config)
        recovery = config.get("recovery_step")
        if recovery is not None and (
            not isinstance(recovery, str)
            or recovery not in steps
            or steps[recovery].kind != "agent"
            or steps[recovery].mutates
        ):
            raise ValueError("Recovery target must be an existing read-only agent step")
        edges = dict(step.transitions)
        if len(edges) != len(step.transitions) or any(t not in steps for t in edges.values()):
            raise ValueError("Duplicate outcome or unknown target")
        if {"waiting", "blocked"} & edges.keys():
            raise ValueError("Reserved outcomes cannot route")
        if step.kind == "finish":
            if edges or step.mutates or step.gate or step.required:
                raise ValueError("Finish cannot execute or be a gate")
        elif not edges:
            raise ValueError("Missing transitions")
        if (
            step.kind in ("agent", "check", "operation")
            and not step.handler
            and not (step.kind == "agent" and step.profile != "default")
        ):
            raise ValueError("Missing handler")
        if step.gate and (
            step.mutates
            or not step.required
            or "passed" not in edges
            or step.kind not in ("agent", "check")
        ):
            raise ValueError("Gate must be required, read-only and produce passed evidence")
        if step.kind == "condition" and (set(edges) != {"true", "false"} or not step.condition_key):
            raise ValueError("Condition needs key and true/false branches")
    for identifier in mandatory:
        if identifier not in steps or not steps[identifier].gate:
            raise ValueError(f"Policy gate missing: {identifier}")

    def reachable(skip: str | None = None) -> set[str]:
        seen: set[str] = set()
        pending = [workflow.entry]
        while pending:
            identifier = pending.pop()
            if identifier == skip or identifier in seen:
                continue
            seen.add(identifier)
            pending.extend(dict(steps[identifier].transitions).values())
        return seen

    if reachable() != steps.keys():
        raise ValueError("Unreachable steps")
    for step in workflow.steps:
        if step.required and finishes & reachable(step.id):
            raise ValueError(f"Completion bypasses required step: {step.id}")
    can_finish = set(finishes)
    while True:
        expanded = can_finish | {
            s.id for s in workflow.steps if any(target in can_finish for _, target in s.transitions)
        }
        if expanded == can_finish:
            break
        can_finish = expanded
    if can_finish != steps.keys():
        raise ValueError("A component has no route to finish")
