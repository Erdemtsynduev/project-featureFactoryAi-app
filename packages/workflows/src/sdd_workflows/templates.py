"""Application flows are ordinary data; they have no special dispatch path."""

from dataclasses import dataclass, replace

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


@dataclass(frozen=True)
class CheckCommand:
    """One project check. `argv[0]` must already be an absolute executable."""

    argv: tuple[str, ...]
    cwd: str = "."
    timeout: int = 900
    title: str = ""


COMMIT_RULE = """
Commits are authorized for this ticket: commit completed work in each owning repository
using that repository's documented commit rules. Never push, rewrite history or touch
repositories outside the ticket's owned paths.
"""


def ticket(
    checks: tuple[CheckCommand, ...],
    implementer: str = "claude",
    reviewer: str = "codex",
    *,
    allow_commits: bool = False,
    max_input_chars: int = 60000,
    isolated: bool = False,
    auto_resolve: bool = True,
) -> Workflow:
    """Execute one approved ticket: implement, every check as its own gate, then review.

    Scope was approved before this run, so there is no planning step. A failed
    check or review goes through diagnosis and bounded repair back to the first check.

    `isolated` runs the ticket in its own worktree lane and merges it back as a
    step: fast-forward only; a moved base means rebase and full re-verification.
    Conflicts go to an agent (`auto_resolve`, for autonomous queues) or to you.
    """
    first = "check_1" if checks else "review"
    extra = COMMIT_RULE if allow_commits else ""
    stages = tuple(
        Step(
            f"check_{index}",
            "check",
            "command",
            transitions=(
                ("passed", f"check_{index + 1}" if index < len(checks) else "review"),
                ("failed", "diagnose"),
            ),
            required=True,
            gate=True,
            max_visits=5,
            timeout=check.timeout,
            config=canonical(
                {
                    "argv": list(check.argv),
                    "cwd": check.cwd,
                    "title": check.title,
                    "recovery_step": "reconcile",
                }
            ),
        )
        for index, check in enumerate(checks, 1)
    )
    recovery = canonical({"recovery_step": "reconcile"})
    return Workflow(
        "ticket",
        "implement",
        (
            Step(
                "implement",
                "agent",
                implementer,
                prompts.IMPLEMENT + extra,
                (("done", first), ("failed", "diagnose"), ("interrupted", "reconcile")),
                required=True,
                mutates=True,
                timeout=3600,
                config=recovery,
            ),
            *stages,
            Step(
                "review",
                "agent",
                reviewer,
                prompts.REVIEW,
                (("passed", "integrate" if isolated else "accepted"), ("failed", "diagnose")),
                required=True,
                gate=True,
                max_visits=5,
                timeout=1800,
                config=recovery,
            ),
            *(_merge_steps(first, implementer, auto_resolve) if isolated else ()),
            Step(
                "diagnose",
                "agent",
                reviewer,
                prompts.DIAGNOSE,
                (("done", "repair"),),
                timeout=1800,
                config=recovery,
            ),
            Step(
                "repair",
                "agent",
                implementer,
                prompts.REPAIR + extra,
                (("done", first), ("failed", "diagnose"), ("interrupted", "reconcile")),
                mutates=True,
                timeout=3600,
                config=recovery,
            ),
            Step(
                "reconcile",
                "agent",
                reviewer,
                prompts.RECONCILE,
                (("done", first), ("failed", "diagnose")),
                timeout=1800,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=14,
        max_input_chars=max_input_chars,
        max_planning_calls=0,
    )


def _merge_steps(first: str, implementer: str, auto_resolve: bool) -> tuple[Step, ...]:
    conflict = "resolve" if auto_resolve else "merge_conflict"
    steps = [
        Step(
            "integrate",
            "operation",
            "lane-integrate",
            transitions=(("merged", "accepted"), ("behind", "rebase")),
            required=True,
            max_visits=6,
            timeout=600,
        ),
        Step(
            "rebase",
            "operation",
            "lane-rebase",
            transitions=(("done", first), ("conflict", conflict)),
            mutates=True,
            max_visits=4,
            timeout=900,
        ),
        Step(
            "merge_conflict",
            "human",
            prompt="Resolve the merge conflict in the lane folder named above, then confirm.",
            transitions=(("resolved", "rebase"),),
        ),
    ]
    if auto_resolve:
        steps.insert(
            2,
            Step(
                "resolve",
                "agent",
                implementer,
                prompts.RESOLVE,
                (("done", first), ("failed", "merge_conflict")),
                mutates=True,
                max_visits=3,
                timeout=3600,
            ),
        )
    return tuple(steps)


def requirement(analyst: str = "codex") -> Workflow:
    """Turn one plan requirement into an approved specification and ticket breakdown.

    The agent may ask structured questions; the human approves the result. It does
    not create ticket runs by itself: approved tickets are admitted explicitly.
    """
    return Workflow(
        "requirement",
        "spec",
        (
            Step(
                "spec",
                "agent",
                analyst,
                prompts.SPEC,
                (("done", "tickets"), ("questions", "interview")),
                required=True,
                config='{"purpose":"planning"}',
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
                analyst,
                prompts.TICKETS,
                (("done", "approve"),),
                required=True,
                config='{"purpose":"planning"}',
            ),
            Step(
                "approve",
                "human",
                prompt="Approve the specification and its ticket breakdown.",
                transitions=(("approved", "accepted"), ("rework", "spec")),
                required=True,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=8,
        max_input_chars=40000,
        max_planning_calls=6,
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


def question_example(language: str = "ru") -> Workflow:
    """One human step with a recommended option; no model calls or project edits."""
    russian = language == "ru"
    prompt = (
        "Какой режим работы выбрать? Рекомендую подтверждение перед изменениями. Опишите ваш выбор."
        if russian
        else "Which working mode should we use? I recommend approval before changes. "
        "Describe your choice."
    )
    options = (
        ["С подтверждением перед изменениями", "Самостоятельно в рамках задачи"]
        if russian
        else ["Approval before changes", "Work within the agreed scope"]
    )
    question = {
        "id": "mode",
        "question": "Режим работы" if russian else "Working mode",
        "options": list[object](options),
        "recommended": options[0],
    }
    return Workflow(
        "interactive-demo",
        "answer",
        (
            Step(
                "answer",
                "human",
                prompt=prompt,
                transitions=(("answered", "finish"),),
                config=canonical({"questions": [question]}),
            ),
            Step("finish", "finish"),
        ),
    )


def command_demo(executable: str) -> Workflow:
    """A single verified check gate: shows the queue without any model."""
    return Workflow(
        "demo",
        "check",
        (
            Step(
                "check",
                "check",
                "command",
                transitions=(("passed", "finish"),),
                required=True,
                gate=True,
                config=canonical({"argv": [executable, "-c", "print('Verified')"]}),
            ),
            Step("finish", "finish"),
        ),
        max_planning_calls=0,
    )


HUMAN_PROMPTS_RU = {
    "interview": "Ответьте на вопросы по спецификации.",
    "answer": "Ответьте на текущий вопрос.",
    "approve": "Подтвердите задачу и критерии приёмки.",
}


def localized(flow: Workflow, language: str) -> Workflow:
    """Human step prompts in the operator's language; agent prompts stay unchanged."""
    if language != "ru":
        return flow
    return replace(
        flow,
        steps=tuple(
            replace(step, prompt=HUMAN_PROMPTS_RU.get(step.id, step.prompt))
            if step.kind == "human"
            else step
            for step in flow.steps
        ),
    )


def with_checks(flow: Workflow, argv: list[str]) -> Workflow:
    """Point every generic command check at the project's own check command."""
    return replace(
        flow,
        steps=tuple(
            replace(step, config=canonical({**object_json(step.config), "argv": list(argv)}))
            if step.kind == "check" and step.handler == "command"
            else step
            for step in flow.steps
        ),
    )
