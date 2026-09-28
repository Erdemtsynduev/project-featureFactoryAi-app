"""Application flows are ordinary data; they have no special dispatch path."""

from sdd_core.models import Step, Workflow


def main_flow(implementer: str = "claude", reviewer: str = "codex", checks: str = "{}") -> Workflow:
    return Workflow(
        "main-flow",
        "spec",
        (
            Step(
                "spec",
                "agent",
                reviewer,
                "Write a bounded specification with explicit acceptance. Return questions for missing user decisions.",
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
                "Describe bounded dependency-ordered tickets covering the entire specification. Do not drop acceptance criteria.",
                (("done", "implement"),),
                required=True,
            ),
            Step(
                "implement",
                "agent",
                implementer,
                "Implement the bounded task, using test-first where meaningful. Preserve acceptance.",
                (("done", "checks"), ("failed", "diagnose")),
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
                "Read-only independent review. Assess Standards and Spec separately, including original scope. Return passed only when both pass.",
                (("passed", "accepted"), ("failed", "diagnose")),
                required=True,
                gate=True,
                max_visits=5,
            ),
            Step(
                "diagnose",
                "agent",
                reviewer,
                "Reproduce the recorded failure and propose a bounded correction. Do not weaken acceptance.",
                (("done", "repair"),),
                max_visits=3,
            ),
            Step(
                "repair",
                "agent",
                implementer,
                "Apply the diagnosed correction. Preserve original acceptance.",
                (("done", "checks"), ("failed", "diagnose")),
                mutates=True,
                max_visits=3,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=16,
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
                "Ask the next necessary question or propose a complete specification.",
                (("questions", "answer"), ("done", "approve")),
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
    )
