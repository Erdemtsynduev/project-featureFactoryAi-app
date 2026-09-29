"""Deterministic control plane. The IO boundary supplies observations, never decisions.

Every rule that changes a run is a pure function `(run, input, now, ids) -> Transition`
here. Each builds the next state and hands it to `changed`, which checks the state
against the status table and the attempt invariants before the version advances.
Why a run is held is the typed `Run.cause`; `Run.reason` is the text people read.
"""

import math
import re
from collections.abc import Callable
from dataclasses import astuple, replace

from sdd_core.models import (
    RECORD_ID,
    REFUSALS,
    STATUS_CHANGES,
    Attempt,
    Cause,
    Command,
    Effect,
    Event,
    EventKind,
    Json,
    Result,
    Run,
    Step,
    Transition,
    Workflow,
)
from sdd_core.wire import canonical, object_json

# Texts people see for the causes the engine sets itself.
WAIT_RETRY_LIMIT = "Wait retry limit"
CALL_LIMIT = "Model call limit"
STOP_REQUESTED = "Stop requested"
PLANNING_LIMIT = "Planning call limit: approve scope or create a revised workflow"
TOKEN_LIMIT = "Token budget exhausted or unknown"
VISIT_LIMIT = "Step visit limit"
QUEUE_LIMIT = "Queue call budget exhausted"
UNCERTAIN = "Process ownership uncertain"
UNSATISFIED = "Unsatisfied gates"
WORKSPACE_CHANGED = "Workspace changed outside attempt"
# Automatic infrastructure retries (lost attempts, provider waits) before a block.
INFRASTRUCTURE_RETRIES = 3
# A provider wait never sleeps past a week; a lost attempt backs off up to a minute.
MAX_WAIT_SECONDS = 7 * 86400
MAX_BACKOFF_SECONDS = 60
# Idle, unfinished states a run can be reconciled from, or restarted from.
RECONCILABLE = ("blocked", "waiting")
RESTARTABLE = ("blocked", "waiting", "ready")

_ATTEMPT_ID = re.compile(RECORD_ID)


def valid_time(now: float) -> None:
    if isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("Time must be finite")


def _consistent(before: Run, after: Run) -> None:
    """The status change is in the table; status, attempt and cause agree.

    Running has an attempt; ready, waiting and accepted have none; blocked may keep
    one whose process ownership is uncertain. A blocked run always says why, and only
    a blocked run carries a rule's cause; a stop request may mark any unfinished run.
    """
    if after.status not in STATUS_CHANGES[before.status]:
        raise ValueError(f"A {before.status} task cannot become {after.status}")
    if after.status == "running" and after.active is None:
        raise ValueError("A running task needs its attempt")
    if after.status in ("ready", "waiting", "accepted") and after.active is not None:
        raise ValueError(f"A {after.status} task cannot hold a live attempt")
    if after.cause not in ("", "stop") and after.status != "blocked":
        raise ValueError(f"Only a blocked task can be held by {after.cause}")
    if after.status == "blocked" and not (after.cause and after.reason):
        raise ValueError("A blocked task needs a cause and a reason")


def changed(
    before: Run,
    after: Run,
    now: float,
    kind: EventKind,
    detail: str = "",
    effects: tuple[Effect, ...] = (),
) -> Transition:
    """The transition from `before` to an already decided `after`. Core-internal.

    Callers outside this module use the named transitions below, so every rule
    that changes a run lives in the pure state machine. Every decided state passes
    through here: its invariants are checked and its version advances exactly once.
    """
    valid_time(now)
    _consistent(before, after)
    state = replace(after, version=before.version + 1)
    return Transition(state, (Event(kind, now, detail),), effects)


def _visits(run: Run, step: str, delta: int) -> tuple[tuple[str, int], ...]:
    """The run's visit counters with `step` moved by `delta`, never below zero."""
    visits = dict(run.visits)
    visits[step] = max(0, visits.get(step, 0) + delta)
    return tuple(sorted(visits.items()))


def _held_by(run: Run, cause: Cause, reason: str) -> Run:
    return replace(run, status="blocked", cause=cause, reason=reason)


def _released(run: Run) -> Run:
    """Nothing holds the run any more; its reason text goes with its cause."""
    return replace(run, cause="", reason="")


def block(
    run: Run,
    now: float,
    reason: str,
    *,
    cause: Cause = "blocked",
    event: EventKind = "blocked",
    detail: str | None = None,
) -> Transition:
    """Stop scheduling a run until an operator retries it."""
    if not reason:
        raise ValueError("Blocker needs a reason")
    if cause in ("", "stop"):
        raise ValueError("A block needs a rule's cause")
    return changed(
        run, _held_by(run, cause, reason), now, event, reason if detail is None else detail
    )


def limit(run: Run, now: float, cause: Cause, reason: str) -> Transition:
    """A budget or visit bound stopped the run before it could start its step."""
    return block(run, now, reason, cause=cause, event="limit", detail="")


def stop_requested(run: Run) -> bool:
    """The operator asked the live attempt to end; the host is settled, not waited on."""
    return run.paused and run.cause == "stop"


def legacy_cause(run: Run) -> Cause:
    """The cause of a run stored before causes existed, read from its reason text."""
    if run.reason == STOP_REQUESTED:
        return "stop"
    if run.status != "blocked":
        return ""
    exact: dict[str, Cause] = {
        CALL_LIMIT: "call_limit",
        PLANNING_LIMIT: "planning_limit",
        TOKEN_LIMIT: "token_limit",
        VISIT_LIMIT: "visit_limit",
        QUEUE_LIMIT: "queue_limit",
        WAIT_RETRY_LIMIT: "wait_limit",
        WORKSPACE_CHANGED: "workspace_changed",
    }
    if run.reason in exact:
        return exact[run.reason]
    if run.reason.startswith(UNCERTAIN):
        return "uncertain"
    if run.reason.startswith(UNSATISFIED):
        return "acceptance"
    return "blocked"


def reconcilable(run: Run) -> bool:
    return run.active is None and run.status in RECONCILABLE


def restartable(run: Run) -> bool:
    return run.active is None and run.status in RESTARTABLE


def invalidate(run: Run, revision: str, now: float) -> Transition:
    """The workspace changed outside an attempt: old gates no longer hold."""
    if run.active is not None:
        raise ValueError("Cannot invalidate an active attempt")
    state = replace(run, revision=revision, gates=())
    held = _held_by(state, "workspace_changed", WORKSPACE_CHANGED)
    return changed(run, held, now, "revision_changed")


def relocate(run: Run, revision: str, now: float, workspace: str) -> Transition:
    """The run moved into its isolated working copy; the new revision is not an outside change."""
    if run.active is not None:
        raise ValueError("Cannot move an active attempt")
    return changed(run, replace(run, revision=revision, gates=()), now, "lane_opened", workspace)


def _rerouted(run: Run, target: str, revision: str, reason: str) -> Run:
    """The run parked at `target`, paused, with nothing pending and no gates."""
    return replace(
        _released(run),
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
    state = _rerouted(run, target, revision, "Reconciliation requested; resume when ready")
    return changed(run, state, now, "reconciliation_requested")


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
    return changed(run, replace(state, infrastructure_failures=0), now, "restarted", target)


def release_condition(run: Run, now: float) -> Transition:
    """Return a condition attempt persisted by an earlier release to pure routing."""
    if run.active is None:
        raise ValueError("No persisted condition attempt")
    state = replace(
        _released(run), active=None, status="ready", visits=_visits(run, run.active.step, -1)
    )
    return changed(run, state, now, "condition_released")


def guidance(run: Run, now: float, message: str) -> Transition:
    """Record operator guidance for the next packet; the run state itself is unchanged."""
    if run.status == "accepted":
        raise ValueError("Accepted tasks cannot receive new execution instructions")
    detail = canonical({"text": message, "after_generation": run.generation})
    return changed(run, run, now, "operator_message", detail)


def _retry(run: Run, now: float, call_grant: int) -> Transition:
    if run.status != "blocked" or run.active is not None:
        raise ValueError("Unsupported command in this state")
    retried = replace(_released(run), status="ready", wake_at=None)
    if run.cause == "call_limit" and call_grant > 0:
        granted = replace(retried, spend=run.spend.grant(call_grant))
        return changed(run, granted, now, "calls_granted", str(call_grant))
    return changed(run, retried, now, "retry_requested")


def _stop(run: Run, now: float, _: int) -> Transition:
    if run.status == "accepted":
        raise ValueError("An accepted task has nothing to stop")
    state = replace(run, paused=True, cause="stop", reason=STOP_REQUESTED)
    return changed(run, state, now, "stop_requested")


def _resume(run: Run, now: float, _: int) -> Transition:
    """Resuming withdraws a pending stop request along with its text. A blocked run
    keeps its cause until it is retried, so it never reads as blocked for no reason."""
    state = _released(run) if run.cause == "stop" and run.status != "blocked" else run
    return changed(run, replace(state, paused=False), now, "resumed")


def _setting(kind: EventKind, **changes: bool) -> Callable[[Run, float, int], Transition]:
    return lambda run, now, _: changed(run, replace(run, **changes), now, kind)  # type: ignore[arg-type]


# One entry per operator command; a new command is a new row, not a new branch.
_COMMANDS: dict[str, Callable[[Run, float, int], Transition]] = {
    "stop": _stop,
    "pause": _setting("paused", paused=True),
    "resume": _resume,
    "auto": _setting("auto_answer_enabled", auto_answer=True),
    "manual": _setting("auto_answer_disabled", auto_answer=False),
    "retry": _retry,
}


def control(run: Run, command: Command | str, now: float, call_grant: int = 0) -> Transition:
    """Apply an operator command. A retry of a run stopped by its model call limit
    grants `call_grant` more calls (the caller passes the workflow's budget)."""
    apply = _COMMANDS.get(command)
    if apply is None:
        raise ValueError("Unsupported command in this state")
    return apply(run, now, call_grant)


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
        and run.spend.calls == 0
        and not run.completed
    )


def holds_claim(run: Run, workflow: Workflow) -> bool:
    """Whether an unfinished run keeps its paths between attempts.

    A run that has dispatched a mutating step may have left partial changes, so
    overlapping work waits until it is accepted. A run that only read (planning,
    questions, approvals, checks) holds nothing between its attempts. A live process
    holds its paths while it runs; a person deciding runs none, so holds nothing.
    """
    if run.status == "accepted":
        return False
    running = run.active is not None and workflow.step(run.active.step).kind != "human"
    visited = dict(run.visits)
    return running or any(step.mutates and step.id in visited for step in workflow.steps)


def unsatisfied(run: Run, workflow: Workflow) -> list[str]:
    """Required steps not yet completed, then gates not passed on the current revision."""
    gates = dict(run.gates)
    missing = [s.id for s in workflow.steps if s.required and s.id not in run.completed]
    stale = [s.id for s in workflow.steps if s.gate and gates.get(s.id) != run.revision]
    return missing + stale


def _finish(run: Run, workflow: Workflow, now: float) -> Transition:
    gaps = unsatisfied(run, workflow)
    if gaps:
        return block(
            run,
            now,
            f"{UNSATISFIED}: {gaps}",
            cause="acceptance",
            event="acceptance_rejected",
            detail="",
        )
    state = replace(_released(run), status="accepted", wake_at=None)
    return changed(run, state, now, "accepted", run.revision)


def _route(run: Run, step: Step, now: float, facts: str) -> Transition:
    """A condition decides the next step inside dispatch, with no attempt or effect."""
    outcome = evaluate(step, facts)
    state = replace(
        _released(run),
        status="ready",
        step=dict(step.transitions)[outcome],  # validation guarantees both branches
        visits=_visits(run, step.id, 1),
        completed=tuple(sorted({*run.completed, step.id})),
        wake_at=None,
    )
    return changed(run, state, now, "condition_evaluated", outcome)


def _over_budget(
    run: Run, workflow: Workflow, step: Step, planning: bool
) -> tuple[Cause, str] | None:
    """Why the run's own budgets forbid another model call, if they do."""
    spend = run.spend
    if (
        planning
        and workflow.max_planning_calls is not None
        and spend.planning_calls >= workflow.max_planning_calls
    ):
        return "planning_limit", PLANNING_LIMIT
    if step.kind != "agent":
        return None
    if spend.calls >= workflow.max_calls + spend.granted_calls:
        return "call_limit", CALL_LIMIT
    if workflow.max_tokens is not None and (
        spend.usage_unknown or spend.tokens >= workflow.max_tokens
    ):
        return "token_limit", TOKEN_LIMIT
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
        return limit(run, now, "visit_limit", VISIT_LIMIT)
    if step.kind == "condition":
        return _route(run, step, now, facts)
    planning = step.kind == "agent" and step.options.planning
    exceeded = _over_budget(run, workflow, step, planning)
    if exceeded is not None:
        return limit(run, now, *exceeded)
    agent = step.kind == "agent"
    attempt = Attempt(
        attempt_id,
        step.id,
        run.generation + 1,
        now,
        now + step.timeout,
        run.revision,
        run.previous_attempt,
        calls=int(agent),
        planning_calls=int(planning),
    )
    state = replace(
        _released(run),
        status="running",
        active=attempt,
        generation=attempt.generation,
        visits=_visits(run, step.id, 1),
        wake_at=None,
        spend=run.spend.reserve(agent, planning),
        gates=() if step.mutates else run.gates,
    )
    effect = Effect(attempt_id, step.kind, attempt)
    return changed(run, state, now, "dispatched", attempt_id, (effect,))


def _wait(run: Run, state: Run, step: Step, result: Result, now: float) -> Transition:
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
        held = _held_by(state, "wait_limit", WAIT_RETRY_LIMIT)
        return changed(run, held, now, "waiting_exhausted", result.reason)
    waiting = replace(
        _released(state), status="waiting", wake_at=result.resume_at, reason=result.reason
    )
    return changed(run, waiting, now, "waiting", result.reason)


def _passed_gate(step: Step, result: Result) -> None:
    """A passing gate needs evidence of the current revision; a review passes both halves."""
    if not result.artifacts or any(a.revision != result.revision for a in result.artifacts):
        raise ValueError("Gate needs evidence bound to current revision")
    if step.kind == "agent" and (result.standards is not True or result.specification is not True):
        raise ValueError("Review must pass Standards and Spec independently")


def _refused(result: Result) -> bool:
    """The provider refused the attempt before any model work: a declared refusal
    with no measured tokens. Its reserved calls go back to the run."""
    if result.outcome not in ("waiting", "blocked"):
        return False
    try:
        failure = object_json(result.data or "{}").get("failure")
    except ValueError:
        return False
    usage = result.usage
    return failure in REFUSALS and not (usage.input_tokens or usage.output_tokens)


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
    refused = _refused(result)
    spend = run.spend.measure(result.usage, step.kind == "agent" and not refused)
    state = replace(
        run,
        active=None,
        previous_attempt=attempt.id,
        revision=result.revision,
        spend=spend.release(attempt) if refused else spend,
    )
    if result.outcome == "waiting":
        return _wait(run, state, step, result, now)
    if result.outcome == "blocked":
        if not result.reason:
            raise ValueError("Blocker needs a reason")
        return changed(
            run, _held_by(state, "blocked", result.reason), now, "blocked", result.reason
        )
    target = step.target(result.outcome)
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
    applied = replace(
        _released(state),
        status="ready",
        step=target,
        gates=tuple(sorted(gates.items())),
        completed=tuple(sorted(completed)),
        infrastructure_failures=0,
    )
    return changed(run, applied, now, "result_applied", result.outcome)


def recover(
    run: Run,
    now: float,
    *,
    termination_confirmed: bool,
    reason: str,
    observed_revision: str,
    max_retries: int = INFRASTRUCTURE_RETRIES,
    recovery_step: str | None = None,
    launched: bool = True,
) -> Transition:
    """Settle a lost attempt. A confirmed retry may reroute to a read-only `recovery_step`.

    `launched=False` is the runtime's proof that the payload never started (no host
    identity, a failed preflight): no model ran, so the reservation is returned and
    token accounting stays known. It still counts against the infrastructure retries.
    """
    if run.active is None:
        raise ValueError("No active attempt")
    if not launched and not termination_confirmed:
        raise ValueError("An attempt that never launched has ended")
    if not termination_confirmed:
        return block(
            run, now, f"{UNCERTAIN}: {reason}", cause="uncertain", event="uncertain", detail=""
        )
    count = run.infrastructure_failures + 1
    state = replace(
        run,
        active=None,
        previous_attempt=run.active.id,
        spend=run.spend.release(run.active)
        if not launched
        else replace(run.spend, usage_unknown=run.spend.usage_unknown or run.spend.calls > 0),
        revision=observed_revision,
        gates=() if observed_revision != run.revision else run.gates,
        infrastructure_failures=count,
        visits=_visits(run, run.step, -1),
    )
    if count > max_retries:
        return changed(
            run, _held_by(state, "recovery_limit", reason), now, "recovery_exhausted", reason
        )
    waiting = replace(
        _released(state),
        status="waiting",
        wake_at=now + min(MAX_BACKOFF_SECONDS, 2**count),
        reason=reason,
    )
    if recovery_step is not None:
        waiting = replace(waiting, step=recovery_step, gates=())
    return changed(run, waiting, now, "recovery_scheduled", reason)


def recovery_target(workflow: Workflow, step: str) -> str | None:
    """The read-only step that `step` declares for reconciliation, if it declares one."""
    target = workflow.step(step).options.recovery_step
    if not target:
        return None
    # `graph.validate` already refuses this; a stored workflow is checked again.
    if workflow.step(target).mutates:
        raise ValueError("Recovery requires a read-only step")
    return target


def recovery_route(run: Run, workflow: Workflow, confirmed: bool, revision: str) -> str | None:
    """Where a lost attempt resumes: its step's recovery step when the attempt is known
    to have ended after changing the workspace; otherwise (None) the same step reruns."""
    touched = run.active is not None and revision != run.active.base_revision
    return recovery_target(workflow, run.step) if confirmed and touched else None


def first_untouched(
    workflow: Workflow,
    attempts: tuple[tuple[str, str], ...],
    results: tuple[tuple[str, str], ...],
    observed: str,
) -> str | None:
    """The first mutating step, when no mutating attempt has changed the workspace.

    True when no mutating step has a processed result and the workspace is at the
    revision every mutating attempt started from; else None (reconcile instead).
    `attempts` are (step, base revision) pairs; `results` are (step, document) pairs.
    """
    mutating = {step.id for step in workflow.steps if step.mutates}
    first = next((step.id for step in workflow.steps if step.mutates), None)
    if first is None or any(step in mutating for step, _ in results):
        return None
    if any(step in mutating and base != observed for step, base in attempts):
        return None
    return first


def human_result(run: Run, outcome: str, text: str, data: str) -> Result:
    """An answer to the run's waiting human step, bound to its attempt and revision."""
    if run.active is None:
        raise ValueError("Not waiting for a human")
    return Result(run.active.id, run.active.generation, outcome, text, run.revision, data=data)


def plan_revised(run: Run, now: float, detail: str) -> Transition:
    """An approved plan review rewrote this never-started ticket's brief or prerequisites."""
    if not discardable(run):
        raise ValueError("Only a ticket that never started can be revised")
    return changed(run, run, now, "plan_revised", detail)
