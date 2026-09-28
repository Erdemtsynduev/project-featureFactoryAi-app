"""Why a task is (not) moving: exactly one reason and the action that resolves it.

The order of the checks is the contract: the reason returned is the one the
operator has to act on first. Every surface (card, drawer, office, inbox) reads
this single derivation instead of re-deriving status from raw fields.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sdd_core.machine import WAIT_RETRY_LIMIT
from sdd_core.models import Run, Step
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
