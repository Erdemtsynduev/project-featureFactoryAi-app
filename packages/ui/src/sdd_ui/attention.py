"""Why a task is (not) moving: exactly one reason and the action that resolves it.

The order of the checks is the contract: the reason returned is the one the
operator has to act on first. Every surface (card, drawer, office, inbox) reads
this single derivation instead of re-deriving status from raw fields.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import cast

from sdd_core.models import Json, Run, Step, Workflow
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
    superseded: str = "",
) -> Attention:
    """`pending` lists dependencies that are not accepted yet; `superseded` names
    the work that took this run's work over."""
    if run.status == "accepted":
        return Attention("accepted", "done")
    if run.active is not None:
        if step.kind == "human":
            return Attention("answer", "attention", "answer", step.prompt)
        return Attention("working", "working")
    if superseded:
        return Attention("superseded", "done", detail=superseded)
    if run.status == "blocked":
        if run.cause == "wait_limit":
            return Attention("limits_exhausted", "blocked", "retry", run.reason)
        if run.cause == "uncertain":
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


@dataclass(frozen=True)
class Progress:
    done: int
    total: int


def rollup(
    own: dict[str, Attention],
    children: dict[str, tuple[str, ...]],
    closed: frozenset[str] = frozenset(),
) -> tuple[dict[str, Attention], dict[str, Progress]]:
    """A parent's reason from its children, at any depth.

    A parent whose own run is still working (writing the specification, waiting
    for approval) keeps its own reason. Once its run is accepted, its planning is
    done and its children say where it stands: delivered when all are done,
    needing you when one does, delivering while one moves, and otherwise paused
    or partially done until the operator resumes the rest or closes it.
    """
    found: dict[str, Attention] = dict(own)
    progress: dict[str, Progress] = {}
    visiting: set[str] = set()

    def resolve(run: str) -> Attention:
        if run in progress or run not in children or run in visiting:
            return found[run]
        visiting.add(run)
        states = [resolve(child) for child in children[run]]
        visiting.discard(run)
        tones = [state.tone for state in states]
        done = tones.count("done")
        count = f"{done}/{len(states)}"
        progress[run] = Progress(done, len(states))
        if own[run].code != "accepted":
            return found[run]
        if run in closed:
            found[run] = Attention("closed", "done", detail=count)
        elif done == len(states):
            found[run] = Attention("delivered", "done", detail=count)
        elif "attention" in tones or "blocked" in tones:
            waiting = sum(tone in ("attention", "blocked") for tone in tones)
            found[run] = Attention("children_need", "attention", "", str(waiting))
        elif "working" in tones:
            found[run] = Attention("delivering", "working", detail=count)
        elif "waiting" in tones:
            found[run] = Attention("delivery_waiting", "waiting", detail=count)
        else:
            code = "partial" if done else "delivery_paused"
            found[run] = Attention(code, "idle", "resume_children", count)
        return found[run]

    for run in children:
        if run in own:
            resolve(run)
    return found, progress


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
                planning += step.options.planning
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
        "calls": run.spend.calls,
        "auto_answer": run.auto_answer,
        "wake_at": run.wake_at,
        "active": active,
    }


def outline(workflow: Workflow) -> dict[str, Json]:
    """A workflow without its prompts: what the board and the office draw."""
    steps = tuple(replace(step, prompt="") for step in workflow.steps)
    return cast(dict[str, Json], asdict(replace(workflow, steps=steps)))
