"""Every guard of the pure state machine, one rule per test.

The lifecycle and property tests drive whole traces; these pin the refusals and
the small predicates the runtime relies on instead of re-deriving them.
"""

from dataclasses import replace

import pytest
from sdd_core import machine
from sdd_core.codec import digest, workflow_json
from sdd_core.models import Artifact, Attempt, Result, Run, Spend, Step, Usage, Workflow


def flow(**budgets: int) -> Workflow:
    return Workflow(
        "rules",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "review"),), mutates=True),
            Step(
                "review",
                "agent",
                "fake",
                transitions=(("passed", "finish"), ("failed", "work")),
                required=True,
                gate=True,
            ),
            Step("finish", "finish"),
        ),
        **budgets,
    )


def ready(workflow: Workflow | None = None, **fields: object) -> Run:
    workflow = workflow or flow()
    run = Run("run", digest(workflow_json(workflow)), workflow.entry, "base", paused=False)
    return replace(run, **fields)


def running(workflow: Workflow | None = None) -> Run:
    workflow = workflow or flow()
    return machine.dispatch(ready(workflow), workflow, 1, "a1").state


def result(run: Run, outcome: str = "done", **fields: object) -> Result:
    assert run.active is not None
    return replace(Result(run.active.id, run.active.generation, outcome, "why", "base"), **fields)


# Predicates the runtime asks instead of repeating the rule.


@pytest.mark.parametrize(
    ("fields", "now", "expected"),
    [
        ({}, 5, True),
        ({"paused": True}, 5, False),
        ({"status": "blocked"}, 5, False),
        ({"status": "accepted"}, 5, False),
        ({"wake_at": 10.0}, 5, False),
        ({"wake_at": 10.0}, 10, True),
        ({"status": "waiting", "wake_at": 4.0}, 5, True),
    ],
)
def test_dispatchable_reads_operator_state_and_timer(fields, now, expected):
    assert machine.dispatchable(ready(**fields), now) is expected


def test_dispatchable_is_false_while_an_attempt_runs():
    assert machine.dispatchable(running(), 5) is False


def test_stop_requested_needs_both_pause_and_the_stop_reason():
    stopped = machine.control(running(), "stop", 2).state
    assert machine.stop_requested(stopped)
    assert not machine.stop_requested(machine.control(running(), "pause", 2).state)
    assert not machine.stop_requested(replace(stopped, paused=False))


@pytest.mark.parametrize(
    ("status", "reconcilable", "restartable"),
    [
        ("blocked", True, True),
        ("waiting", True, True),
        ("ready", False, True),
        ("running", False, False),
        ("accepted", False, False),
    ],
)
def test_recovery_predicates_follow_the_run_status(status, reconcilable, restartable):
    run = ready(status=status)
    assert machine.reconcilable(run) is reconcilable
    assert machine.restartable(run) is restartable


def test_an_active_attempt_is_never_reconciled_or_restarted():
    run = replace(running(), status="blocked")
    assert not machine.reconcilable(run) and not machine.restartable(run)
    with pytest.raises(ValueError, match="blocked or waiting"):
        machine.reconcile(run, "review", "base", 2)
    with pytest.raises(ValueError, match="inactive, unfinished"):
        machine.restart(run, "work", "base", 2)


def test_unsatisfied_lists_missing_required_steps_then_stale_gates():
    run = ready(completed=("review",), gates=(("review", "old"),))
    assert machine.unsatisfied(run, flow()) == ["review"]
    assert machine.unsatisfied(ready(), flow()) == ["review", "review"]
    assert (
        machine.unsatisfied(ready(completed=("review",), gates=(("review", "base"),)), flow()) == []
    )


def test_holds_claim_releases_on_acceptance_and_after_read_only_work():
    workflow = flow()
    assert machine.holds_claim(running(workflow), workflow)
    assert not machine.holds_claim(ready(), workflow)
    assert not machine.holds_claim(ready(status="accepted", visits=(("work", 1),)), workflow)
    assert not machine.holds_claim(ready(visits=(("review", 1),)), workflow)


# Refusals.


def test_invalidate_and_relocate_refuse_a_live_attempt():
    with pytest.raises(ValueError, match="invalidate an active"):
        machine.invalidate(running(), "new", 2)
    with pytest.raises(ValueError, match="move an active"):
        machine.relocate(running(), "new", 2, "lane")


def test_release_condition_needs_a_persisted_attempt():
    with pytest.raises(ValueError, match="No persisted condition"):
        machine.release_condition(ready(), 1)


def test_unknown_or_misplaced_commands_are_refused():
    with pytest.raises(ValueError, match="Unsupported command"):
        machine.control(ready(), "explode", 1)
    with pytest.raises(ValueError, match="Unsupported command"):
        machine.control(ready(), "retry", 1)  # only a blocked, idle run retries


def test_retry_grants_calls_only_after_the_call_limit():
    blocked = machine.block(ready(), 1, "other reason").state
    retried = machine.control(blocked, "retry", 2, call_grant=5)
    assert retried.events[0].kind == "retry_requested" and retried.state.spend.granted_calls == 0
    limited = machine.block(ready(), 1, machine.CALL_LIMIT, cause="call_limit").state
    granted = machine.control(limited, "retry", 2, call_grant=5)
    assert granted.events[0].kind == "calls_granted" and granted.state.spend.granted_calls == 5


@pytest.mark.parametrize("attempt", ["", "has space", "x" * 97, "semi;colon"])
def test_dispatch_refuses_malformed_attempt_ids(attempt):
    with pytest.raises(ValueError, match="Invalid attempt id"):
        machine.dispatch(ready(), flow(), 1, attempt)


def test_dispatch_refuses_held_runs_and_early_timers():
    with pytest.raises(ValueError, match="not dispatchable"):
        machine.dispatch(ready(paused=True), flow(), 1, "a")
    with pytest.raises(ValueError, match="Timer has not fired"):
        machine.dispatch(ready(wake_at=9.0), flow(), 1, "a")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True])
def test_time_must_be_a_finite_number(now):
    with pytest.raises(ValueError, match="finite"):
        machine.dispatch(ready(), flow(), now, "a")


def test_budgets_stop_dispatch_with_a_limit_event():
    workflow = flow(max_calls=1)
    spent = ready(workflow, spend=Spend(calls=1))
    limited = machine.dispatch(spent, workflow, 1, "a")
    assert limited.state.cause == "call_limit" and limited.events[0].kind == "limit"
    assert limited.state.reason == machine.CALL_LIMIT
    tokens = flow(max_tokens=10)
    unknown = machine.dispatch(ready(tokens, spend=Spend(usage_unknown=True)), tokens, 1, "a")
    assert unknown.state.reason == "Token budget exhausted or unknown"
    visited = machine.dispatch(ready(visits=(("work", 3),)), flow(), 1, "a")
    assert visited.state.reason == "Step visit limit" and not visited.effects


def test_complete_refuses_stale_late_and_dishonest_results():
    run = running()
    with pytest.raises(ValueError, match="Stale attempt"):
        machine.complete(run, flow(), result(run, generation=9), 2)
    with pytest.raises(ValueError, match="Late result"):
        machine.complete(run, flow(), result(run), 10_000)
    with pytest.raises(ValueError, match="Negative usage"):
        machine.complete(run, flow(), result(run, usage=Usage(-1, 1)), 2)
    with pytest.raises(ValueError, match="not declared"):
        machine.complete(run, flow(), result(run, "invented"), 2)
    with pytest.raises(ValueError, match="Blocker needs a reason"):
        machine.complete(run, flow(), result(run, "blocked", reason=""), 2)


def test_a_read_only_step_may_not_move_the_revision():
    workflow = flow()
    run = machine.complete(running(workflow), workflow, result(running(workflow)), 2).state
    review = machine.dispatch(run, workflow, 3, "a2").state
    with pytest.raises(ValueError, match="Read-only step changed"):
        machine.complete(review, workflow, result(review, "passed", revision="moved"), 4)


def test_a_passing_review_needs_evidence_and_both_verdicts():
    workflow = flow()
    run = machine.complete(running(workflow), workflow, result(running(workflow)), 2).state
    review = machine.dispatch(run, workflow, 3, "a2").state
    evidence = (Artifact("review.json", "h", "base"),)
    with pytest.raises(ValueError, match="Standards and Spec"):
        machine.complete(
            review, workflow, result(review, "passed", artifacts=evidence, standards=True), 4
        )
    passed = machine.complete(
        review,
        workflow,
        result(review, "passed", artifacts=evidence, standards=True, specification=True),
        4,
    ).state
    assert dict(passed.gates) == {"review": "base"} and "review" in passed.completed


@pytest.mark.parametrize("resume_at", [None, 1.0, 2.0 + machine.MAX_WAIT_SECONDS + 1])
def test_a_wait_needs_a_bounded_future_wake_time(resume_at):
    run = running()
    with pytest.raises(ValueError, match="bounded future wake time"):
        machine.complete(run, flow(), result(run, "waiting", resume_at=resume_at), 2)


def test_waits_give_back_the_visit_and_block_after_the_retry_budget():
    workflow = flow()
    run = running(workflow)
    for attempt in range(machine.INFRASTRUCTURE_RETRIES):
        waited = machine.complete(run, workflow, result(run, "waiting", resume_at=50.0), 2)
        assert waited.state.status == "waiting" and dict(waited.state.visits)["work"] == 0
        run = machine.dispatch(waited.state, workflow, 60, f"w{attempt}").state
    exhausted = machine.complete(run, workflow, result(run, "waiting", resume_at=100.0), 61)
    assert exhausted.state.reason == machine.WAIT_RETRY_LIMIT
    assert exhausted.events[0].kind == "waiting_exhausted"


def test_recover_needs_an_attempt_and_blocks_on_uncertain_ownership():
    with pytest.raises(ValueError, match="No active attempt"):
        machine.recover(ready(), 1, termination_confirmed=True, reason="x", observed_revision="b")
    uncertain = machine.recover(
        running(), 2, termination_confirmed=False, reason="host alive", observed_revision="base"
    )
    assert uncertain.state.status == "blocked" and uncertain.state.active is not None
    assert uncertain.state.reason == "Process ownership uncertain: host alive"


def test_recover_backs_off_and_clears_gates_when_the_revision_moved():
    run = replace(running(), gates=(("review", "base"),))
    lost = machine.recover(
        run, 2, termination_confirmed=True, reason="lost", observed_revision="moved"
    ).state
    assert lost.status == "waiting" and lost.wake_at == 2 + 2 and lost.gates == ()
    assert dict(lost.visits)["work"] == 0 and lost.spend.usage_unknown
    rerouted = machine.recover(
        run,
        2,
        termination_confirmed=True,
        reason="lost",
        observed_revision="base",
        recovery_step="review",
    ).state
    assert rerouted.step == "review" and rerouted.gates == ()


def test_guidance_is_refused_once_accepted():
    with pytest.raises(ValueError, match="Accepted tasks"):
        machine.guidance(ready(status="accepted"), 1, "more")


def test_restart_rewinds_to_the_target_with_a_fresh_retry_budget():
    run = ready(status="blocked", infrastructure_failures=3, step="review", gates=(("x", "y"),))
    restarted = machine.restart(run, "work", "fresh", 5)
    assert restarted.state.step == "work" and restarted.state.paused
    assert restarted.state.infrastructure_failures == 0 and restarted.state.gates == ()
    assert restarted.events[0].kind == "restarted" and restarted.events[0].detail == "work"


def test_release_condition_gives_back_its_visit():
    attempt = Attempt("c1", "route", 1, 0, 10, "base")
    run = ready(active=attempt, status="running", visits=(("route", 1),))
    released = machine.release_condition(run, 2).state
    assert released.active is None and dict(released.visits)["route"] == 0
