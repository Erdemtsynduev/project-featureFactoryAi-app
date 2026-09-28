from dataclasses import replace

import pytest
from sdd_core.codec import (
    digest,
    result_json,
    result_load,
    run_json,
    run_load,
    workflow_json,
    workflow_load,
)
from sdd_core.graph import validate
from sdd_core.machine import complete, control, dispatch, recover
from sdd_core.models import Artifact, Result, Run, Step, Usage, Workflow
from sdd_workflows.templates import interview, main_flow


def flow():
    return Workflow(
        "test",
        "work",
        (
            Step(
                "work",
                "operation",
                "fake",
                transitions=(("done", "check"),),
                mutates=True,
                required=True,
            ),
            Step(
                "check",
                "check",
                "fake",
                transitions=(("passed", "finish"),),
                required=True,
                gate=True,
            ),
            Step("finish", "finish"),
        ),
    )


def ready(workflow=None):
    workflow = workflow or flow()
    return Run("run", digest(workflow_json(workflow)), workflow.entry, "base", paused=False)


def test_roundtrip_and_deterministic_dispatch():
    f = flow()
    validate(f)
    assert workflow_load(workflow_json(f)) == f
    a = dispatch(ready(), f, 10, "attempt")
    assert a == dispatch(ready(), f, 10, "attempt")
    assert run_load(run_json(a.state)) == a.state
    result = Result("attempt", 1, "done", "implemented", "new")
    assert result_load(result_json(result)) == result


def test_acceptance_requires_current_gate():
    f = flow()
    a = dispatch(ready(), f, 1, "work").state
    a = complete(a, f, Result("work", 1, "done", "ok", "new"), 2).state
    a = dispatch(a, f, 3, "check").state
    with pytest.raises(ValueError, match="evidence"):
        complete(a, f, Result("check", 2, "passed", "ok", "new"), 4)
    a = complete(
        a, f, Result("check", 2, "passed", "ok", "new", (Artifact("test.log", "hash", "new"),)), 4
    ).state
    assert dispatch(a, f, 5, "unused").state.status == "accepted"
    assert dispatch(replace(a, revision="changed"), f, 5, "unused").state.status == "blocked"


def test_required_custom_step_cannot_be_bypassed():
    f = flow()
    extra = Step(
        "docs", "agent", "fake", "Write docs", (("done", "check"),), required=True, mutates=True
    )
    f = replace(
        f, steps=(replace(f.steps[0], transitions=(("done", "docs"),)), extra, *f.steps[1:])
    )
    validate(f)
    with pytest.raises(ValueError, match="bypasses"):
        validate(
            replace(
                f,
                steps=(
                    replace(f.steps[0], transitions=(("done", "docs"), ("skip", "check"))),
                    *f.steps[1:],
                ),
            )
        )


@pytest.mark.parametrize("template", [main_flow, interview])
def test_application_templates_are_regular_graphs(template):
    validate(template())


def test_stale_late_and_readonly_result_rejected():
    f = flow()
    active = dispatch(ready(), f, 0, "one").state
    for result, now in [
        (Result("old", 1, "done", "", "base"), 1),
        (Result("one", 2, "done", "", "base"), 1),
        (Result("one", 1, "done", "", "base"), 901),
    ]:
        with pytest.raises(ValueError):
            complete(active, f, result, now)
    check = replace(active, step="check", active=replace(active.active, step="check"))
    with pytest.raises(ValueError, match="Read-only"):
        complete(check, f, Result("one", 1, "passed", "", "changed"), 1)


def test_pause_survives_recovery_and_unknown_process_keeps_ownership():
    f = flow()
    active = dispatch(ready(), f, 0, "one").state
    paused = control(active, "pause", 1).state
    uncertain = recover(
        paused, 2, termination_confirmed=False, reason="ACL", observed_revision="base"
    ).state
    assert uncertain.active == paused.active
    assert uncertain.paused
    recovered = recover(
        uncertain, 3, termination_confirmed=True, reason="exited", observed_revision="new"
    ).state
    assert recovered.active is None and recovered.paused
    assert dict(recovered.visits)["work"] == 0
    assert recovered.wake_at == 5


def test_budget_unknown_blocks_only_when_configured():
    f = Workflow(
        "budget",
        "agent",
        (
            Step("agent", "agent", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
        max_tokens=100,
    )
    r = replace(ready(f), usage_unknown=True)
    assert dispatch(r, f, 0, "one").state.status == "blocked"
    assert dispatch(r, replace(f, max_tokens=None), 0, "one").state.active is not None


def test_review_requires_both_axes():
    f = Workflow(
        "review",
        "review",
        (
            Step(
                "review",
                "agent",
                "fake",
                transitions=(("passed", "finish"),),
                required=True,
                gate=True,
            ),
            Step("finish", "finish"),
        ),
    )
    active = dispatch(ready(f), f, 0, "a").state
    result = Result(
        "a",
        1,
        "passed",
        "ok",
        "base",
        (Artifact("report", "hash", "base"),),
        Usage(1, 1),
        True,
        False,
    )
    with pytest.raises(ValueError, match="Standards"):
        complete(active, f, result, 1)


def test_bounded_recovery_does_not_reset_on_retry():
    f = flow()
    r = ready(f)
    for n in range(4):
        r = dispatch(r, f, n * 100, str(n)).state
        r = recover(
            r, n * 100 + 1, termination_confirmed=True, reason="network", observed_revision="base"
        ).state
    assert r.status == "blocked" and r.infrastructure_failures == 4


@pytest.mark.parametrize(
    "raw",
    [
        '{"id":"x","entry":"x","steps":[],"schema":true}',
        '{"id":"x","entry":"x","steps":[],"surprise":1}',
        '{"steps":NaN}',
    ],
)
def test_bad_wire_contract(raw):
    with pytest.raises(ValueError):
        workflow_load(raw)
