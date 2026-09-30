"""Editor operations are pure functions over versioned graph documents."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from difflib import unified_diff
from typing import Any

from sdd_core.codec import (
    canonical,
    decode,
    flag,
    mapping,
    object_json,
    sequence,
    text,
    workflow_json,
)
from sdd_core.graph import validate
from sdd_core.models import Json, Step, Workflow
from sdd_core.options import StepOptions


def insert_step(
    workflow: Workflow,
    source: str,
    outcome: str,
    step: Step,
    *,
    additional_edges: tuple[tuple[str, str], ...] = (),
) -> Workflow:
    existing = workflow.step(source)
    edges = dict(existing.transitions)
    if outcome not in edges or step.id in {s.id for s in workflow.steps}:
        raise ValueError("Invalid insertion")
    if not step.transitions:
        step = replace(step, transitions=(("passed" if step.gate else "done", edges[outcome]),))
    replacements = {(source, outcome), *additional_edges}
    for node, result in replacements:
        if dict(workflow.step(node).transitions).get(result) != edges[outcome]:
            raise ValueError("Explicit insertion edges must share a target")
    edited = replace(
        workflow,
        steps=tuple(
            replace(
                s,
                transitions=tuple(
                    (result, step.id if (s.id, result) in replacements else target)
                    for result, target in s.transitions
                ),
            )
            for s in workflow.steps
        )
        + (step,),
    )
    validate(edited)
    return edited


def compare(left: Workflow, right: Workflow) -> str:
    import json

    a = json.dumps(json.loads(workflow_json(left)), indent=2, ensure_ascii=False).splitlines()
    b = json.dumps(json.loads(workflow_json(right)), indent=2, ensure_ascii=False).splitlines()
    return "\n".join(unified_diff(a, b, fromfile="published", tofile="draft"))


def simulate(workflow: Workflow, outcomes: tuple[str, ...]) -> tuple[str, ...]:
    """Inspect a route without executing tools or pretending gates passed."""
    validate(workflow)
    route = [workflow.entry]
    visits: dict[str, int] = {}
    for outcome in outcomes:
        step = workflow.step(route[-1])
        visits[step.id] = visits.get(step.id, 0) + 1
        if visits[step.id] > step.max_visits:
            raise ValueError("Simulation exceeds step limit")
        if outcome not in dict(step.transitions):
            raise ValueError("Outcome not declared")
        route.append(dict(step.transitions)[outcome])
    return tuple(route)


# Changes to a workflow version --------------------------------------------------------
#
# A published workflow never changes; a flow change produces a new version and says
# where each removed step's work continues, so a running run can migrate onto it
# (see `machine.migrate`).


@dataclass(frozen=True)
class Skip:
    """Route around a step and drop it. A required step is skipped only on a person's
    explicit decision (`required=True`)."""

    step: str
    required: bool = False


@dataclass(frozen=True)
class Insert:
    """Add `step` on the `outcome` route of `after`."""

    after: str
    outcome: str
    step: Step


@dataclass(frozen=True)
class SetProfile:
    """Run an agent step with another agent profile."""

    step: str
    profile: str


@dataclass(frozen=True)
class SetOption:
    """Change one option of a step's configuration (see `options.StepOptions`)."""

    step: str
    key: str
    value: Json


type FlowChange = Skip | Insert | SetProfile | SetOption

# The outcomes a skipped step's work continues on, in order of preference.
SKIP_ROUTES = ("passed", "done")


def _skip(workflow: Workflow, change: Skip) -> tuple[Workflow, dict[str, str]]:
    step = workflow.step(change.step)
    if step.kind == "finish":
        raise ValueError("The finish cannot be skipped")
    if step.required and not change.required:
        raise ValueError(f"{step.id} is required: skipping it needs a person's decision")
    edges = dict(step.transitions)
    routes = [edges[outcome] for outcome in SKIP_ROUTES if outcome in edges]
    if not routes and len(edges) == 1:
        routes = list(edges.values())
    if not routes or routes[0] == step.id:
        raise ValueError(f"{step.id} has no single route to continue on")
    target = routes[0]
    kept = tuple(
        replace(
            other,
            transitions=tuple(
                (outcome, target if to == step.id else to) for outcome, to in other.transitions
            ),
        )
        for other in workflow.steps
        if other.id != step.id
    )
    entry = target if workflow.entry == step.id else workflow.entry
    return replace(workflow, entry=entry, steps=kept), {step.id: target}


def _set_step(workflow: Workflow, identifier: str, **fields: object) -> Workflow:
    workflow.step(identifier)  # an unknown step is refused
    return replace(
        workflow,
        steps=tuple(
            replace(step, **fields) if step.id == identifier else step  # type: ignore[arg-type]
            for step in workflow.steps
        ),
    )


def _insert(workflow: Workflow, change: Insert) -> tuple[Workflow, dict[str, str]]:
    return insert_step(workflow, change.after, change.outcome, change.step), {}


def _profile(workflow: Workflow, change: SetProfile) -> tuple[Workflow, dict[str, str]]:
    if workflow.step(change.step).kind != "agent":
        raise ValueError(f"Only an agent step has a profile: {change.step}")
    return _set_step(workflow, change.step, profile=change.profile), {}


def _option(workflow: Workflow, change: SetOption) -> tuple[Workflow, dict[str, str]]:
    config = {**object_json(workflow.step(change.step).config), change.key: change.value}
    options = StepOptions.parse(canonical(config))
    if options.unknown:
        raise ValueError(f"Unknown step option: {', '.join(options.unknown)}")
    return _set_step(workflow, change.step, config=options.render()), {}


# One rule per kind of change: a new kind is a new row.
_APPLY: dict[type, Callable[[Workflow, Any], tuple[Workflow, dict[str, str]]]] = {
    Skip: _skip,
    Insert: _insert,
    SetProfile: _profile,
    SetOption: _option,
}


def changed_flow(
    workflow: Workflow, changes: tuple[FlowChange, ...]
) -> tuple[Workflow, dict[str, str]]:
    """The workflow with `changes` applied in order, validated, and where each removed
    step's work continues. The flow keeps its id: it is a new version of the same flow."""
    if not changes:
        raise ValueError("A flow change needs at least one change")
    moved: dict[str, str] = {}
    for change in changes:
        workflow, placed = _APPLY[type(change)](workflow, change)
        moved = {old: placed.get(new, new) for old, new in moved.items()} | placed
    validate(workflow)
    return workflow, moved


READERS: dict[str, Callable[[Mapping[str, Json]], FlowChange]] = {
    "skip": lambda item: Skip(
        text(item.get("step"), "step"), flag(item.get("required", False), "required")
    ),
    "insert": lambda item: Insert(
        text(item.get("after"), "after"),
        text(item.get("outcome"), "outcome"),
        decode(Step, item.get("step")),
    ),
    "profile": lambda item: SetProfile(
        text(item.get("step"), "step"), text(item.get("profile"), "profile")
    ),
    "option": lambda item: SetOption(
        text(item.get("step"), "step"), text(item.get("key"), "key"), item.get("value")
    ),
}


def flow_changes_of(raw: Json) -> tuple[FlowChange, ...]:
    """Flow changes from their JSON list: each an object with a `kind` from `READERS`."""
    changes: list[FlowChange] = []
    for entry in sequence(raw):
        item = mapping(entry)
        kind = text(item.get("kind"), "kind")
        if kind not in READERS:
            raise ValueError(f"Unknown flow change: {kind}")
        changes.append(READERS[kind](item))
    return tuple(changes)
