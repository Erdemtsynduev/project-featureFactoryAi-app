"""Application flows are ordinary data; they have no special dispatch path."""

from dataclasses import dataclass, replace

from sdd_core.codec import canonical, object_json
from sdd_core.models import Step, Workflow

from sdd_workflows import prompts


@dataclass(frozen=True)
class Roles:
    """The agent profile that plays each role in a project's flows."""

    # Cuts a draft into features and revises a feature's tickets when one is stuck.
    lead: str = "codex"
    # Writes specifications and ticket breakdowns.
    analyst: str = "codex"
    implementer: str = "claude"
    # Reviews, diagnoses and reconciles.
    reviewer: str = "codex"


DEFAULT_ROLES = Roles()


def main_flow(roles: Roles = DEFAULT_ROLES, checks: str = "{}") -> Workflow:
    analyst, implementer, reviewer = roles.analyst, roles.implementer, roles.reviewer
    flow = Workflow(
        "main-flow",
        "spec",
        (
            Step(
                "spec",
                "agent",
                analyst,
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
                analyst,
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

LANE_RULE = """
This ticket runs in its own lane. Do not commit and do not edit the files that pin other
repositories (such as .gitmodules): after each step the engine commits your work and
advances the pins of dependencies that earlier tickets delivered. Dependencies are checked
out where this repository links them; change them only through their own tickets.
"""


def ticket(
    checks: tuple[CheckCommand, ...],
    roles: Roles = DEFAULT_ROLES,
    *,
    allow_commits: bool = False,
    max_input_chars: int = 60000,
    isolated: bool = False,
    auto_resolve: bool = True,
    commit_messages: tuple[str, str] = ("", ""),
) -> Workflow:
    """Execute one approved ticket: implement, every check as its own gate, then review.

    Scope was approved before this run, so there is no planning step. A failed
    check or review goes through diagnosis and bounded repair back to the first check.

    `isolated` runs the ticket in its own worktree lane and merges it back as a
    step: fast-forward only; a moved base means rebase and full re-verification.
    Conflicts go to an agent (`auto_resolve`, for autonomous queues) or to you.
    In a lane the engine commits after every agent pass (`commit`), so checks and review
    see committed work; `commit_messages` are the project's (work, pin) templates.
    """
    implementer, reviewer = roles.implementer, roles.reviewer
    verify = "check_1" if checks else "review"
    first = "commit" if isolated else verify
    # An explicit commit authorization wins; a lane otherwise leaves committing to the engine.
    extra = COMMIT_RULE if allow_commits else LANE_RULE if isolated else ""
    lane_config = canonical(
        {
            key: value
            for key, value in zip(("commit_message", "pin_message"), commit_messages, strict=True)
            if value
        }
    )
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
            *(
                (
                    Step(
                        "commit",
                        "operation",
                        "lane-commit",
                        transitions=(("done", verify),),
                        required=True,
                        mutates=True,
                        max_visits=12,
                        timeout=600,
                        config=lane_config,
                    ),
                    *_merge_steps(first, implementer, auto_resolve, lane_config),
                )
                if isolated
                else ()
            ),
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


def _merge_steps(
    first: str, implementer: str, auto_resolve: bool, lane_config: str = "{}"
) -> tuple[Step, ...]:
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
            config=lane_config,
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


# What no ticket agent can do, whatever tools its steps grant.
AGENT_LIMITS = (
    "No agent can buy, license or download gated files, sign in to services, or ask a "
    "person mid-step; such work needs a person (`human`) or an asset (`asset`)."
)


def capabilities(flow: Workflow) -> str:
    """What the agents of `flow` can do, derived from its steps (one source of truth)."""
    lines = []
    for step in flow.steps:
        if step.kind != "agent":
            continue
        work = "edits files and runs commands" if step.mutates else "reads only"
        web = "has web access" if "web" in step.options.tools else "has no internet"
        lines.append(f"- {step.id}: {work}; {web}.")
    return "\n".join([*lines, AGENT_LIMITS])


def with_capabilities(prompt: str, agents: str) -> str:
    return prompt + ("\nTicket agents:\n" + agents + "\n" if agents else "")


def with_tools(flow: Workflow, tools: tuple[str, ...]) -> Workflow:
    """Grant `tools` to every agent step of `flow` (a project allowing the web)."""
    if not tools:
        return flow
    return replace(
        flow,
        steps=tuple(
            replace(step, config=step.options.changed(tools=tools).render())
            if step.kind == "agent"
            else step
            for step in flow.steps
        ),
    )


def draft(roles: Roles = DEFAULT_ROLES) -> Workflow:
    """Cut one draft into features: the lead proposes them, a person approves.

    The application admits the approved features as paused child runs of the feature
    flow, never from the agent's own decision. Nothing here writes a specification.
    """
    return Workflow(
        "draft",
        "groom",
        (
            Step(
                "groom",
                "agent",
                roles.lead,
                prompts.GROOM,
                (("done", "approve"), ("questions", "interview")),
                required=True,
                config='{"produces":"features","purpose":"planning"}',
            ),
            Step(
                "interview",
                "human",
                prompt="Resolve the lead's questions about this draft.",
                transitions=(("answered", "groom"),),
            ),
            Step(
                "approve",
                "human",
                prompt="Approve the features cut from this draft. "
                "Approved features appear on the board as paused feature tasks.",
                transitions=(("approved", "accepted"), ("rework", "groom")),
                required=True,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=4,
        # A draft's brief lists every open row of its source.
        max_input_chars=60000,
        max_planning_calls=4,
    )


def feature(roles: Roles = DEFAULT_ROLES, agents: str = "") -> Workflow:
    """Turn one feature into an approved specification (PRD) and ticket breakdown.

    The agent may ask structured questions; the human approves the result. The
    tickets step declares structured tickets; the application admits them as child
    ticket runs only after the approval, never from the agent's own decision.
    """
    analyst = roles.analyst
    return Workflow(
        "feature",
        "spec",
        (
            Step(
                "spec",
                "agent",
                analyst,
                prompts.SPEC,
                (("done", "tickets"), ("questions", "interview")),
                required=True,
                config='{"produces":"specification","purpose":"planning"}',
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
                with_capabilities(prompts.BREAKDOWN, agents),
                (("done", "approve"),),
                required=True,
                config='{"produces":"tickets","purpose":"planning"}',
            ),
            Step(
                "approve",
                "human",
                prompt="Approve the specification and its ticket breakdown. "
                "Approved tickets appear on the board as paused ticket tasks.",
                transitions=(("approved", "accepted"), ("rework", "spec")),
                required=True,
            ),
            Step("accepted", "finish"),
        ),
        max_calls=8,
        # A feature's brief lists a whole plan's open rows and recorded drafts.
        max_input_chars=60000,
        max_planning_calls=6,
    )


def plan_review(roles: Roles = DEFAULT_ROLES) -> Workflow:
    """The lead's revision of a feature's approved tickets: proposals a person decides on.

    The agent only proposes (`plan_changes`); the application applies an approved
    proposal, so no plan changes on an agent's own decision. The brief carries the
    ticket agents' capabilities.
    """
    return Workflow(
        "plan-review",
        "replan",
        (
            Step(
                "replan",
                "agent",
                roles.lead,
                prompts.PLAN_REVIEW,
                (("done", "decide"), ("unchanged", "accepted")),
                config='{"produces":"plan_changes","purpose":"planning"}',
            ),
            Step(
                "decide",
                "human",
                prompt="Decide on the proposed plan changes. Approved changes are applied "
                "to the plan's tickets; rejected ones leave it as it is.",
                transitions=(
                    ("approved", "accepted"),
                    ("rejected", "accepted"),
                    ("rework", "replan"),
                ),
            ),
            Step("accepted", "finish"),
        ),
        max_calls=4,
        max_input_chars=60000,
        max_planning_calls=4,
    )


def approved_feature(roles: Roles = DEFAULT_ROLES) -> Workflow:
    """Execute an explicitly approved feature without planning: approval, then build."""
    flow = main_flow(roles)
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
        id="approved-feature",
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
    "decide": "Решите по предложенным изменениям плана. Одобренные применяются к тикетам "
    "плана; отклонённые оставляют его как есть.",
    # A flow's own wording of a step, as `<flow>.<step>`, wins over the shared one.
    "draft.interview": "Ответьте на вопросы лида по черновику.",
    "draft.approve": "Одобрите фичи, нарезанные из черновика. Одобренные появятся на доске "
    "как фичи на паузе.",
}


def localized(flow: Workflow, language: str) -> Workflow:
    """Human step prompts in the operator's language; agent prompts stay unchanged."""
    if language != "ru":
        return flow

    def prompt(step: Step) -> str:
        own = HUMAN_PROMPTS_RU.get(f"{flow.id}.{step.id}")
        return own or HUMAN_PROMPTS_RU.get(step.id, step.prompt)

    return replace(
        flow,
        steps=tuple(
            replace(step, prompt=prompt(step)) if step.kind == "human" else step
            for step in flow.steps
        ),
    )


def with_checks(flow: Workflow, argv: list[str]) -> Workflow:
    """Point every unconfigured command check at the project's own check command.

    Checks that already name a command (a ticket's repository checks) keep it.
    Without a project command the unconfigured checks are bypassed: each route into
    such a check goes to its `passed` target instead, so review still gates work.
    """
    unconfigured = {
        s.id
        for s in flow.steps
        if s.kind == "check" and s.handler == "command" and not s.options.argv
    }
    if argv:
        return replace(
            flow,
            steps=tuple(
                replace(
                    step,
                    config=step.options.changed(argv=tuple(argv)).render(),
                )
                if step.id in unconfigured
                else step
                for step in flow.steps
            ),
        )
    if not unconfigured:
        return flow

    def target(step_id: str) -> str:
        seen = set()
        while step_id in unconfigured and step_id not in seen:
            seen.add(step_id)
            step_id = dict(flow.step(step_id).transitions)["passed"]
        return step_id

    return replace(
        flow,
        entry=target(flow.entry),
        steps=tuple(
            replace(step, transitions=tuple((o, target(t)) for o, t in step.transitions))
            for step in flow.steps
            if step.id not in unconfigured
        ),
    )
