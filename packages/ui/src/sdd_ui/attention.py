"""Why a task is (not) moving: exactly one reason and the action that resolves it.

The order of the checks is the contract: the reason returned is the one the
operator has to act on first. Every surface (card, drawer, office, inbox) reads
this single derivation instead of re-deriving status from raw fields.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import cast

from sdd_core.machine import WAIT_RETRY_LIMIT
from sdd_core.models import Json, Run, Step, Workflow
from sdd_core.options import StepOptions
from sdd_core.sdk import handler_key


@dataclass(frozen=True)
class Attention:
    code: str  # stable key the UI translates
    tone: str  # done | working | waiting | attention | blocked | idle
    action: str = ""  # command that resolves it, if one exists
    detail: str = ""
    until: float | None = None


def attention(
    run: Run,
    step: Step,
    *,
    queue_running: bool,
    pending: tuple[str, ...],
    has_profile: Callable[[str], bool],
    available: Callable[[str], bool],
) -> Attention:
    """`pending` lists dependencies that are not accepted yet."""
    if run.status == "accepted":
        return Attention("accepted", "done")
    if run.active is not None:
        if step.kind == "human":
            return Attention("answer", "attention", "answer", step.prompt)
        return Attention("working", "working")
    if run.status == "blocked":
        if run.reason == WAIT_RETRY_LIMIT:
            return Attention("limits_exhausted", "blocked", "retry", run.reason)
        if run.reason.startswith("Process ownership uncertain"):
            return Attention("uncertain", "blocked", "recover", run.reason)
        return Attention("blocked", "blocked", "retry", run.reason)
    if step.kind == "agent":
        profile = handler_key(step)
        if not has_profile(profile):
            return Attention("no_profile", "blocked", "agents", profile)
        if not available(profile):
            return Attention("profile_resting", "waiting", "", profile)
    if run.status == "waiting":
        return Attention("waiting", "waiting", "", run.reason, run.wake_at)
    if run.paused:
        return Attention("paused", "idle", "resume")
    if pending:
        return Attention("dependencies", "waiting", "", ", ".join(pending))
    if not queue_running:
        return Attention("queue_paused", "idle", "queue")
    return Attention("queued", "working")


def lane(tone: str) -> str:
    """The board column of an attention tone: queue, running, needs (you) or done."""
    if tone == "done":
        return "done"
    if tone in ("attention", "blocked"):
        return "needs"
    if tone == "working":
        return "running"
    return "queue"


def calls_needed(run: Run, workflow: Workflow) -> dict[str, int]:
    """Least model calls the run still needs: its required agent steps not yet done.

    An estimate for budgets, not a promise: retries and repairs cost more.
    """
    calls = planning = 0
    done = set(run.completed)
    if run.status != "accepted":
        for step in workflow.steps:
            if step.kind == "agent" and step.required and step.id not in done:
                calls += 1
                planning += StepOptions.parse(step.config).planning
    return {"calls": calls, "planning": planning}


def projection(run: Run) -> dict[str, Json]:
    """What the board shows of a run; the drawer loads the whole run separately."""
    active: Json = None
    if run.active is not None:
        active = {
            "step": run.active.step,
            "started": run.active.started,
            "deadline": run.active.deadline,
        }
    return {
        "id": run.id,
        "status": run.status,
        "paused": run.paused,
        "step": run.step,
        "workflow_digest": run.workflow_digest,
        "version": run.version,
        "reason": run.reason,
        "calls": run.calls,
        "auto_answer": run.auto_answer,
        "wake_at": run.wake_at,
        "active": active,
    }


def outline(workflow: Workflow) -> dict[str, Json]:
    """A workflow without its prompts: what the board and the office draw."""
    steps = tuple(replace(step, prompt="") for step in workflow.steps)
    return cast(dict[str, Json], asdict(replace(workflow, steps=steps)))
