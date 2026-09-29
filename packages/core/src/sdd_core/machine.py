"""Deterministic control plane. The IO boundary supplies observations, never decisions."""

import math
import re
from dataclasses import astuple, replace

from sdd_core.codec import canonical, object_json
from sdd_core.models import Attempt, Effect, Event, Json, Result, Run, Step, Transition, Workflow
from sdd_core.options import StepOptions

# Consecutive infrastructure waits ended in a block; the operator (or a revival
# policy once limits have reset) retries explicitly.
WAIT_RETRY_LIMIT = "Wait retry limit"
CALL_LIMIT = "Model call limit"
STOP_REQUESTED = "Stop requested"
# Automatic infrastructure retries (lost attempts, provider waits) before a block.
INFRASTRUCTURE_RETRIES = 3
# A provider wait never sleeps past a week; a lost attempt backs off up to a minute.
MAX_WAIT_SECONDS = 7 * 86400
MAX_BACKOFF_SECONDS = 60
# Idle, unfinished states a run can be reconciled from, or restarted from.
RECONCILABLE = ("blocked", "waiting")
RESTARTABLE = ("blocked", "waiting", "ready")

_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")


def valid_time(now: float) -> None:
    if isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("Time must be finite")


def changed(run: Run, now: float, kind: str, detail: str = "") -> Transition:
    """Build a transition from an already decided state. Core-internal.

    Callers outside this module use the named transitions below, so every rule
    that changes a run lives in the pure state machine.
    """
    valid_time(now)
    return Transition(replace(run, version=run.version + 1), (Event(kind, now, detail),))


def _visits(run: Run, step: str, delta: int) -> tuple[tuple[str, int], ...]:
    """The run's visit counters with `step` moved by `delta`, never below zero."""
    visits = dict(run.visits)
    visits[step] = max(0, visits.get(step, 0) + delta)
    return tuple(sorted(visits.items()))


def block(
    run: Run, now: float, reason: str, *, event: str = "blocked", detail: str | None = None
) -> Transition:
    """Stop scheduling a run until an operator retries it."""
    if not reason:
        raise ValueError("Blocker needs a reason")
    return changed(
        replace(run, status="blocked", reason=reason),
        now,
        event,
        reason if detail is None else detail,
    )


def _limit(run: Run, now: float, reason: str) -> Transition:
    """A budget or visit bound stopped the run before it could start its step."""
    return block(run, now, reason, event="limit", detail="")


def stop_requested(run: Run) -> bool:
    """The operator asked the live attempt to end; the host is settled, not waited on."""
    return run.paused and run.reason == STOP_REQUESTED


def reconcilable(run: Run) -> bool:
    return run.active is None and run.status in RECONCILABLE


def restartable(run: Run) -> bool:
    return run.active is None and run.status in RESTARTABLE


def invalidate(run: Run, revision: str, now: float) -> Transition:
    """The workspace changed outside an attempt: old gates no longer hold."""
    if run.active is not None:
        raise ValueError("Cannot invalidate an active attempt")
    return changed(
        replace(
            run,
            revision=revision,
            gates=(),
            status="blocked",
            reason="Workspace changed outside attempt",
        ),
        now,
        "revision_changed",
    )


def relocate(run: Run, revision: str, now: float, workspace: str) -> Transition:
    """The run moved into its isolated working copy; the new revision is not an outside change."""
    if run.active is not None:
        raise ValueError("Cannot move an active attempt")
    return changed(replace(run, revision=revision, gates=()), now, "lane_opened", workspace)


def _rerouted(run: Run, target: str, revision: str, reason: str) -> Run:
    """The run parked at `target`, paused, with nothing pending and no gates."""
    return replace(
        run,
        step=target,
        revision=revision,
        status="ready",
        paused=True,
        wake_at=None,
        gates=(),
        reason=reason,
    )


def reconcile(run: Run, target: str, revision: str, now: float) -> Transition:
    """Route an inactive blocked or waiting run to its read-only recovery step, paused."""
    if not reconcilable(run):
        raise ValueError("Recovery requires an inactive blocked or waiting task")
    return changed(
        _rerouted(run, target, revision, "Reconciliation requested; resume when ready"),
        now,
        "reconciliation_requested",
    )


def restart(run: Run, target: str, revision: str, now: float) -> Transition:
    """Send an inactive blocked or waiting run back to its first mutating step, paused.

    The caller has established that no mutating attempt changed the workspace, so
    there is nothing to reconcile; the infrastructure retry budget starts afresh.
    """
    if not restartable(run):
        raise ValueError("Restart requires an inactive, unfinished task")
    state = _rerouted(
        run, target, revision, "Nothing was changed yet: restarted from the first working step"
    )
    return changed(replace(state, infrastructure_failures=0), now, "restarted", target)


def release_condition(run: Run, now: float) -> Transition:
    """Return a condition attempt persisted by an earlier release to pure routing."""
    if run.active is None:
        raise ValueError("No persisted condition attempt")
    return changed(
        replace(run, active=None, status="ready", visits=_visits(run, run.active.step, -1)),
        now,
        "condition_released",
    )


def guidance(run: Run, now: float, message: str) -> Transition:
    """Record operator guidance for the next packet; the run state itself is unchanged."""
    if run.status == "accepted":
        raise ValueError("Accepted tasks cannot receive new execution instructions")
    return changed(
        run,
        now,
        "operator_message",
        canonical({"text": message, "after_generation": run.generation}),
    )


def control(run: Run, command: str, now: float, call_grant: int = 0) -> Transition:
    """Apply an operator command. A retry of a run stopped by its model call limit
    grants `call_grant` more calls (the caller passes the workflow's budget)."""
    if command == "stop":
        return changed(replace(run, paused=True, reason=STOP_REQUESTED), now, "stop_requested")
    if command == "pause":
        return changed(replace(run, paused=True), now, "paused")
    if command == "resume":
        return changed(replace(run, paused=False), now, "resumed")
    if command in ("auto", "manual"):
        return changed(
            replace(run, auto_answer=command == "auto"),
            now,
            "auto_answer_enabled" if command == "auto" else "auto_answer_disabled",
        )
    if command == "retry" and run.status == "blocked" and run.active is None:
        retried = replace(run, status="ready", reason="", wake_at=None)
        if run.reason == CALL_LIMIT and call_grant > 0:
            granted = replace(retried, granted_calls=run.granted_calls + call_grant)
            return changed(granted, now, "calls_granted", str(call_grant))
        return changed(retried, now, "retry_requested")
    raise ValueError("Unsupported command in this state")


def evaluate(step: Step, facts: str) -> str:
    """Route a condition on the previous result's data; missing keys are false.

    `condition_key` is a dotted path into the JSON object. Text compares
    verbatim; other JSON values compare by their canonical JSON spelling.
    """
    value: Json = object_json(facts)
    for part in step.condition_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return "false"
        value = value[part]
    spelled = value if isinstance(value, str) else canonical(value)
    return "true" if spelled == step.condition_value else "false"


def _held(run: Run) -> bool:
    """Paused, running, blocked or finished: no step may start whatever the time."""
    return run.paused or run.active is not None or run.status in ("blocked", "accepted")


def _sleeping(run: Run, now: float) -> bool:
    return run.wake_at is not None and run.wake_at > now


def dispatchable(run: Run, now: float) -> bool:
    """Whether the operator state lets the current step start now."""
    return not (_held(run) or _sleeping(run, now))


def discardable(run: Run) -> bool:
    """A run that never started work leaves nothing behind when it is removed."""
    return (
        run.status != "accepted"
        and run.generation == 0
        and run.active is None
        and run.calls == 0
        and not run.completed
    )


def holds_claim(run: Run, workflow: Workflow) -> bool:
    """Whether an unfinished run keeps its paths between attempts.

    A run that has dispatched a mutating step may have left partial changes, so
    overlapping work waits until it is accepted. A run that only read (planning,
    questions, approvals, checks) holds nothing between its attempts.
    """
    if run.status == "accepted":
        return False
    visited = dict(run.visits)
    return run.active is not None or any(
        step.mutates and step.id in visited for step in workflow.steps
    )


def unsatisfied(run: Run, workflow: Workflow) -> list[str]:
    """Required steps not yet completed, then gates not passed on the current revision."""
    gates = dict(run.gates)
    missing = [s.id for s in workflow.steps if s.required and s.id not in run.completed]
    stale = [s.id for s in workflow.steps if s.gate and gates.get(s.id) != run.revision]
    return missing + stale


def _finish(run: Run, workflow: Workflow, now: float) -> Transition:
    gaps = unsatisfied(run, workflow)
    if gaps:
        return block(run, now, f"Unsatisfied gates: {gaps}", event="acceptance_rejected", detail="")
    return changed(
        replace(run, status="accepted", wake_at=None, reason=""), now, "accepted", run.revision
    )


def _route(run: Run, step: Step, now: float, facts: str) -> Transition:
    """A condition decides the next step inside dispatch, with no attempt or effect."""
    outcome = evaluate(step, facts)
    return changed(
        replace(
            run,
            status="ready",
            step=dict(step.transitions)[outcome],
            visits=_visits(run, step.id, 1),
            completed=tuple(sorted({*run.completed, step.id})),
            wake_at=None,
            reason="",
        ),
        now,
        "condition_evaluated",
        outcome,
    )


def _over_budget(run: Run, workflow: Workflow, step: Step, planning: bool) -> str | None:
    """Why the run's own budgets forbid another model call, if they do."""
    if (
        planning
        and workflow.max_planning_calls is not None
        and run.planning_calls >= workflow.max_planning_calls
    ):
        return "Planning call limit: approve scope or create a revised workflow"
    if step.kind != "agent":
        return None
    if run.calls >= workflow.max_calls + run.granted_calls:
        return CALL_LIMIT
    if workflow.max_tokens is not None and (run.usage_unknown or run.tokens >= workflow.max_tokens):
        return "Token budget exhausted or unknown"
    return None


def dispatch(
    run: Run, workflow: Workflow, now: float, attempt_id: str, facts: str = "{}"
) -> Transition:
    """Start the current step. Conditions route in-place from `facts` without an effect."""
    valid_time(now)
    if not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("Invalid attempt id")
    if _held(run):
        raise ValueError("Run is not dispatchable")
    if _sleeping(run, now):
        raise ValueError("Timer has not fired")
    step = workflow.step(run.step)
    if step.kind == "finish":
        return _finish(run, workflow, now)
    if dict(run.visits).get(step.id, 0) >= step.max_visits:
        return _limit(run, now, "Step visit limit")
    if step.kind == "condition":
        return _route(run, step, now, facts)
    planning = step.kind == "agent" and StepOptions.parse(step.config).planning
    exceeded = _over_budget(run, workflow, step, planning)
    if exceeded is not None:
        return _limit(run, now, exceeded)
    attempt = Attempt(
        attempt_id,
        step.id,
        run.generation + 1,
        now,
        now + step.timeout,
        run.revision,
        run.previous_attempt,
    )
    state = replace(
        run,
        status="running",
        active=attempt,
        generation=attempt.generation,
        visits=_visits(run, step.id, 1),
        wake_at=None,
        reason="",
        calls=run.calls + int(step.kind == "agent"),
        planning_calls=run.planning_calls + int(planning),
        gates=() if step.mutates else run.gates,
        version=run.version + 1,
    )
    return Transition(
        state, (Event("dispatched", now, attempt_id),), (Effect(attempt_id, step.kind, attempt),)
    )


def _wait(state: Run, step: Step, result: Result, now: float) -> Transition:
    """An infrastructure wait: it consumes no product visit, only the retry budget."""
    if (
        not result.reason
        or result.resume_at is None
        or not (now < result.resume_at <= now + MAX_WAIT_SECONDS)
    ):
        raise ValueError("Waiting requires reason and bounded future wake time")
    failures = state.infrastructure_failures + 1
    state = replace(state, visits=_visits(state, step.id, -1), infrastructure_failures=failures)
    if failures > INFRASTRUCTURE_RETRIES:
        return block(state, now, WAIT_RETRY_LIMIT, event="waiting_exhausted", detail=result.reason)
    return changed(
        replace(state, status="waiting", wake_at=result.resume_at, reason=result.reason),
        now,
        "waiting",
        result.reason,
    )


def _passed_gate(step: Step, result: Result) -> None:
    """A passing gate needs evidence of the current revision; a review passes both halves."""
    if not result.artifacts or any(a.revision != result.revision for a in result.artifacts):
        raise ValueError("Gate needs evidence bound to current revision")
    if step.kind == "agent" and (result.standards is not True or result.specification is not True):
        raise ValueError("Review must pass Standards and Spec independently")


def complete(run: Run, workflow: Workflow, result: Result, now: float) -> Transition:
    valid_time(now)
    attempt = run.active
    if (
        attempt is None
        or result.attempt_id != attempt.id
        or result.generation != attempt.generation
    ):
        raise ValueError("Stale attempt")
    step = workflow.step(attempt.step)
    if step.kind != "human" and now > attempt.deadline:
        raise ValueError("Late result requires reconciliation")
    if not step.mutates and result.revision != attempt.base_revision:
        raise ValueError("Read-only step changed the revision")
    if any(v is not None and v < 0 for v in astuple(result.usage)):
        raise ValueError("Negative usage")
    usage = result.usage
    state = replace(
        run,
        active=None,
        previous_attempt=attempt.id,
        revision=result.revision,
        tokens=run.tokens + (usage.input_tokens or 0) + (usage.output_tokens or 0),
        usage_unknown=run.usage_unknown
        or (step.kind == "agent" and (usage.input_tokens is None or usage.output_tokens is None)),
    )
    if result.outcome == "waiting":
        return _wait(state, step, result, now)
    if result.outcome == "blocked":
        return block(state, now, result.reason)
    target = dict(step.transitions).get(result.outcome)
    if target is None:
        raise ValueError("Outcome is not declared")
    gates = dict(state.gates)
    completed = set(state.completed)
    if step.gate:
        gates.pop(step.id, None)
        if result.outcome == "passed":
            _passed_gate(step, result)
            gates[step.id] = result.revision
    if not step.gate or result.outcome == "passed":
        completed.add(step.id)
    return changed(
        replace(
            state,
            status="ready",
            step=target,
            gates=tuple(sorted(gates.items())),
            completed=tuple(sorted(completed)),
            infrastructure_failures=0,
            reason="",
        ),
        now,
        "result_applied",
        result.outcome,
    )


def recover(
    run: Run,
    now: float,
    *,
    termination_confirmed: bool,
    reason: str,
    observed_revision: str,
    max_retries: int = INFRASTRUCTURE_RETRIES,
    recovery_step: str | None = None,
) -> Transition:
    """Settle a lost attempt. A confirmed retry may reroute to a read-only `recovery_step`."""
    if run.active is None:
        raise ValueError("No active attempt")
    if not termination_confirmed:
        return block(
            run, now, "Process ownership uncertain: " + reason, event="uncertain", detail=""
        )
    count = run.infrastructure_failures + 1
    state = replace(
        run,
        active=None,
        previous_attempt=run.active.id,
        usage_unknown=run.usage_unknown or run.calls > 0,
        revision=observed_revision,
        gates=() if observed_revision != run.revision else run.gates,
        infrastructure_failures=count,
        visits=_visits(run, run.step, -1),
        reason=reason,
    )
    if count > max_retries:
        return changed(replace(state, status="blocked"), now, "recovery_exhausted", reason)
    waiting = replace(state, status="waiting", wake_at=now + min(MAX_BACKOFF_SECONDS, 2**count))
    if recovery_step is not None:
        waiting = replace(waiting, step=recovery_step, gates=())
    return changed(waiting, now, "recovery_scheduled", reason)
