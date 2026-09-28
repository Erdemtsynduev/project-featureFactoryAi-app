"""Application flows are ordinary data; they have no special dispatch path."""

from dataclasses import replace

from sdd_core.codec import canonical, object_json
from sdd_core.models import Step, Workflow

from sdd_workflows import prompts


def main_flow(implementer: str = "claude", reviewer: str = "codex", checks: str = "{}") -> Workflow:
    flow = Workflow(
        "main-flow",
        "spec",
        (
            Step(
                "spec",
                "agent",
                reviewer,
                prompts.SPEC,
                (("done", "tickets"), ("questions", "interview")),
                required=True,
            ),
            Step(
                "interview",
                "human",
                prompt="Resolve the specification questions.",
                transitions=(("answered", "spec"),),
            ),
            Step(
                "tickets",
                "agent",
                reviewer,
                prompts.TICKETS,
                (("done", "implement"),),
                required=True,
            ),
            Step(
                "implement",
                "agent",
                implementer,
                prompts.IMPLEMENT,
                (("done", "checks"), ("failed", "diagnose"), ("interrupted", "reconcile")),
                required=True,
                mutates=True,
            ),
            Step(
                "checks",
                "check",
                "command",
                transitions=(("passed", "review"), ("failed", "diagnose")),
                required=True,
                gate=True,
                max_visits=5,
                config=checks,
            ),
            Step(
                "review",
                "agent",
                reviewer,
                prompts.REVIEW,
                (("passed", "accepted"), ("failed", "diagnose")),
                required=True,
                gate=True,
                max_visits=5,
            ),
            Step(
                "diagnose",
                "agent",
                reviewer,
                prompts.DIAGNOSE,
                (("done", "repair"),),
                max_visits=3,
            ),
            Step(
                "repair",
                "agent",
                implementer,
                prompts.REPAIR,
                (("done", "checks"), ("failed", "diagnose"), ("interrupted", "reconcile")),
                mutates=True,
                max_visits=3,
            ),
            Step(
                "reconcile",
                "agent",
                reviewer,
                prompts.RECONCILE,
                (("done", "checks"), ("failed", "diagnose")),
                max_visits=3,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=16,
    )

    return replace(
        flow,
        max_planning_calls=4,
        steps=tuple(
            replace(step, config='{"purpose":"planning"}')
            if step.id in ("spec", "tickets")
            else replace(
                step, config=canonical({**object_json(step.config), "recovery_step": "reconcile"})
            )
            if step.id in ("implement", "checks", "review", "diagnose", "repair", "reconcile")
            else step
            for step in flow.steps
        ),
    )


def interview(provider: str = "claude") -> Workflow:
    return Workflow(
        "interview",
        "ask",
        (
            Step(
                "ask",
                "agent",
                provider,
                prompts.INTERVIEW,
                (("questions", "answer"), ("done", "approve")),
                config='{"purpose":"planning"}',
            ),
            Step(
                "answer",
                "human",
                prompt="Answer the current question.",
                transitions=(("answered", "ask"),),
            ),
            Step(
                "approve",
                "human",
                prompt="Approve the proposed specification.",
                transitions=(("approved", "finish"),),
                required=True,
            ),
            Step("finish", "finish"),
        ),
        max_planning_calls=4,
    )


def feature() -> Workflow:
    """Execute an explicitly approved requirement without a portfolio audit loop."""
    flow = main_flow()
    steps = tuple(step for step in flow.steps if step.id not in ("spec", "tickets", "interview"))
    approval = Step(
        "approve",
        "human",
        prompt="Confirm this bounded requirement and its acceptance criteria",
        transitions=(("approved", "implement"),),
        required=True,
    )
    return replace(
        flow,
        id="feature",
        entry="approve",
        steps=(approval, *steps),
        max_calls=8,
        max_planning_calls=0,
    )
