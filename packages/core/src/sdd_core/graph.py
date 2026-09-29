"""Validate executable semantics independently of editor layout.

Validation runs as named rules in a fixed order: document bounds, each step on its
own, the policy's mandatory gates, then the shape of the whole graph. The first
broken rule raises `ValueError` with the reason.
"""

import re
from collections.abc import Iterable, Mapping

from sdd_core.models import KINDS, PROCESS_KINDS, RESERVED_OUTCOMES, STEP_ID, Step, Workflow

MAX_STEPS = 128


def validate(workflow: Workflow, mandatory: tuple[str, ...] = ()) -> None:
    _check_document(workflow)
    steps = {s.id: s for s in workflow.steps}
    if len(steps) != len(workflow.steps) or workflow.entry not in steps:
        raise ValueError("Duplicate step or missing entry")
    if not any(s.kind == "finish" for s in workflow.steps):
        raise ValueError("No finish")
    for step in workflow.steps:
        _check_step(step, steps)
    for identifier in mandatory:
        if identifier not in steps or not steps[identifier].gate:
            raise ValueError(f"Policy gate missing: {identifier}")
    _check_shape(workflow, steps)


def _check_document(workflow: Workflow) -> None:
    if workflow.schema != 1 or not 1 <= len(workflow.steps) <= MAX_STEPS:
        raise ValueError("Unsupported schema or step count")
    if workflow.max_calls < 1 or workflow.max_input_chars < 128:
        raise ValueError("Invalid budgets")
    if workflow.max_tokens is not None and workflow.max_tokens < 1:
        raise ValueError("Invalid token budget")
    if workflow.max_planning_calls is not None and workflow.max_planning_calls < 0:
        raise ValueError("Invalid planning call budget")


def _check_step(step: Step, steps: Mapping[str, Step]) -> None:
    if not re.fullmatch(STEP_ID, step.id):
        raise ValueError("Invalid step id")
    if step.kind not in KINDS:
        raise ValueError("Unknown kind")
    if not 1 <= step.max_visits <= 100 or not 1 <= step.timeout <= 86400:
        raise ValueError("Unbounded step")
    settings = step.options
    if settings.unknown:
        raise ValueError(f"Unknown step options on {step.id}: {', '.join(settings.unknown)}")
    recovery = settings.recovery_step
    if recovery and (
        recovery not in steps or steps[recovery].kind != "agent" or steps[recovery].mutates
    ):
        raise ValueError("Recovery target must be an existing read-only agent step")
    edges = dict(step.transitions)
    if len(edges) != len(step.transitions) or any(t not in steps for t in edges.values()):
        raise ValueError("Duplicate outcome or unknown target")
    if set(RESERVED_OUTCOMES) & edges.keys():
        raise ValueError("Reserved outcomes cannot route")
    if step.kind == "finish":
        if edges or step.mutates or step.gate or step.required:
            raise ValueError("Finish cannot execute or be a gate")
    elif not edges:
        raise ValueError("Missing transitions")
    if (
        step.kind in PROCESS_KINDS
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


def _check_shape(workflow: Workflow, steps: Mapping[str, Step]) -> None:
    """Every step is reachable, required steps cannot be bypassed, and all routes can finish."""
    finishes = {s.id for s in workflow.steps if s.kind == "finish"}
    if reachable(steps, workflow.entry) != steps.keys():
        raise ValueError("Unreachable steps")
    for step in workflow.steps:
        if step.required and finishes & reachable(steps, workflow.entry, skip=step.id):
            raise ValueError(f"Completion bypasses required step: {step.id}")
    if _reaching(steps, finishes) != steps.keys():
        raise ValueError("A component has no route to finish")


def reachable(steps: Mapping[str, Step], entry: str, skip: str | None = None) -> set[str]:
    """Steps a run can visit from `entry` without passing through `skip`."""
    seen: set[str] = set()
    pending = [entry]
    while pending:
        identifier = pending.pop()
        if identifier == skip or identifier in seen:
            continue
        seen.add(identifier)
        pending.extend(dict(steps[identifier].transitions).values())
    return seen


def _reaching(steps: Mapping[str, Step], targets: Iterable[str]) -> set[str]:
    """Steps with a route into `targets` (the targets included)."""
    found = set(targets)
    while True:
        expanded = found | {
            s.id for s in steps.values() if any(target in found for _, target in s.transitions)
        }
        if expanded == found:
            return found
        found = expanded


def dependency_layers(
    dependencies: Mapping[str, tuple[str, ...]], error: str
) -> tuple[tuple[str, ...], ...]:
    """Kahn layering: each layer depends only on earlier layers, in mapping order.

    Raises `ValueError(error)` when a dependency is cyclic or names no node.
    """
    layers: list[tuple[str, ...]] = []
    done: set[str] = set()
    while len(done) < len(dependencies):
        ready = tuple(
            node for node, needs in dependencies.items() if node not in done and set(needs) <= done
        )
        if not ready:
            raise ValueError(error)
        layers.append(ready)
        done.update(ready)
    return tuple(layers)
