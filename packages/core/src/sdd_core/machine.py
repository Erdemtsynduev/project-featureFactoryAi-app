"""Deterministic control plane. The IO boundary supplies observations, never decisions."""

import math
import re
from dataclasses import replace

from sdd_core.models import Attempt, Effect, Event, Result, Run, Transition, Workflow


def valid_time(now: float) -> None:
    if isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("Time must be finite")


def changed(run: Run, now: float, kind: str, detail: str = "") -> Transition:
    valid_time(now)
    return Transition(replace(run, version=run.version + 1), (Event(kind, now, detail),))


def control(run: Run, command: str, now: float) -> Transition:
    if command == "stop":
        return changed(replace(run, paused=True, reason="Stop requested"), now, "stop_requested")
    if command == "pause":
        return changed(replace(run, paused=True), now, "paused")
    if command == "resume":
        return changed(replace(run, paused=False), now, "resumed")
    if command == "retry" and run.status == "blocked" and run.active is None:
        return changed(
            replace(run, status="ready", reason="", wake_at=None), now, "retry_requested"
        )
    raise ValueError("Unsupported command in this state")


def dispatch(run: Run, workflow: Workflow, now: float, attempt_id: str) -> Transition:
    valid_time(now)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", attempt_id):
        raise ValueError("Invalid attempt id")
    if run.paused or run.active or run.status in ("blocked", "accepted"):
        raise ValueError("Run is not dispatchable")
    if run.wake_at is not None and run.wake_at > now:
        raise ValueError("Timer has not fired")
    step = workflow.step(run.step)
    if step.kind == "finish":
        missing = [s.id for s in workflow.steps if s.required and s.id not in run.completed]
        stale = [
            s.id for s in workflow.steps if s.gate and dict(run.gates).get(s.id) != run.revision
        ]
        if missing or stale:
            return changed(
                replace(run, status="blocked", reason=f"Unsatisfied gates: {missing + stale}"),
                now,
                "acceptance_rejected",
            )
        return changed(
            replace(run, status="accepted", wake_at=None, reason=""), now, "accepted", run.revision
        )
    visits = dict(run.visits)
    if visits.get(step.id, 0) >= step.max_visits:
        return changed(replace(run, status="blocked", reason="Step visit limit"), now, "limit")
    if step.kind == "agent":
        if run.calls >= workflow.max_calls:
            return changed(replace(run, status="blocked", reason="Model call limit"), now, "limit")
        if workflow.max_tokens is not None and (
            run.usage_unknown or run.tokens >= workflow.max_tokens
        ):
            return changed(
                replace(run, status="blocked", reason="Token budget exhausted or unknown"),
                now,
                "limit",
            )
    attempt = Attempt(
        attempt_id,
        step.id,
        run.generation + 1,
        now,
        now + step.timeout,
        run.revision,
        run.previous_attempt,
    )
    visits[step.id] = visits.get(step.id, 0) + 1
    state = replace(
        run,
        status="running",
        active=attempt,
        generation=attempt.generation,
        visits=tuple(sorted(visits.items())),
        wake_at=None,
        reason="",
        calls=run.calls + int(step.kind == "agent"),
        gates=() if step.mutates else run.gates,
        version=run.version + 1,
    )
    return Transition(
        state, (Event("dispatched", now, attempt_id),), (Effect(attempt_id, step.kind, attempt),)
    )


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
    if any(
        v is not None and v < 0
        for v in (
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.cache_read,
            result.usage.cache_write,
        )
    ):
        raise ValueError("Negative usage")
    state = replace(
        run,
        active=None,
        previous_attempt=attempt.id,
        revision=result.revision,
        tokens=run.tokens + (result.usage.input_tokens or 0) + (result.usage.output_tokens or 0),
        usage_unknown=run.usage_unknown
        or (
            step.kind == "agent"
            and (result.usage.input_tokens is None or result.usage.output_tokens is None)
        ),
    )
    if result.outcome == "waiting":
        if (
            not result.reason
            or result.resume_at is None
            or not now < result.resume_at <= now + 604800
        ):
            raise ValueError("Waiting requires reason and bounded future wake time")
        # Infrastructure wait does not consume product correction visits.
        visits = dict(state.visits)
        visits[step.id] -= 1
        failures = state.infrastructure_failures + 1
        if failures > 3:
            return changed(
                replace(
                    state,
                    status="blocked",
                    reason="Wait retry limit",
                    infrastructure_failures=failures,
                    visits=tuple(sorted(visits.items())),
                ),
                now,
                "waiting_exhausted",
                result.reason,
            )
        return changed(
            replace(
                state,
                status="waiting",
                wake_at=result.resume_at,
                reason=result.reason,
                visits=tuple(sorted(visits.items())),
                infrastructure_failures=failures,
            ),
            now,
            "waiting",
            result.reason,
        )
    if result.outcome == "blocked":
        if not result.reason:
            raise ValueError("Blocker needs a reason")
        return changed(
            replace(state, status="blocked", reason=result.reason), now, "blocked", result.reason
        )
    target = dict(step.transitions).get(result.outcome)
    if target is None:
        raise ValueError("Outcome is not declared")
    gates = dict(state.gates)
    if step.gate:
        gates.pop(step.id, None)
    completed = set(state.completed)
    if step.gate and result.outcome == "passed":
        if not result.artifacts or any(a.revision != result.revision for a in result.artifacts):
            raise ValueError("Gate needs evidence bound to current revision")
        if step.kind == "agent" and (
            result.standards is not True or result.specification is not True
        ):
            raise ValueError("Review must pass Standards and Spec independently")
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
    max_retries: int = 3,
) -> Transition:
    if run.active is None:
        raise ValueError("No active attempt")
    if not termination_confirmed:
        return changed(
            replace(run, status="blocked", reason="Process ownership uncertain: " + reason),
            now,
            "uncertain",
        )
    count = run.infrastructure_failures + 1
    visits = dict(run.visits)
    visits[run.step] -= 1
    state = replace(
        run,
        active=None,
        previous_attempt=run.active.id,
        usage_unknown=run.usage_unknown or run.calls > 0,
        revision=observed_revision,
        gates=() if observed_revision != run.revision else run.gates,
        infrastructure_failures=count,
        visits=tuple(sorted(visits.items())),
        reason=reason,
    )
    if count > max_retries:
        return changed(replace(state, status="blocked"), now, "recovery_exhausted", reason)
    return changed(
        replace(state, status="waiting", wake_at=now + min(60, 2**count)),
        now,
        "recovery_scheduled",
        reason,
    )
