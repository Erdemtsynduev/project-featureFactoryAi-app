"""Editor operations are pure functions over versioned graph documents."""

from dataclasses import replace
from difflib import unified_diff

from sdd_core.codec import workflow_json
from sdd_core.graph import validate
from sdd_core.models import Step, Workflow


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
