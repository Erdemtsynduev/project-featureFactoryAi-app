"""Generated traces exercise safety invariants, not implementation snapshots."""

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sdd_core.editor import insert_step
from sdd_core.evidence import durable_evidence
from sdd_core.graph import validate
from sdd_core.machine import complete, control, dispatch, recover
from sdd_core.models import Artifact, Result, Run, Step, Workflow


def loop_flow(limit=3):
    return Workflow(
        "loop",
        "work",
        (
            Step(
                "work",
                "operation",
                "test",
                transitions=(("again", "work"), ("done", "finish")),
                max_visits=limit,
            ),
            Step("finish", "finish"),
        ),
    )


@settings(max_examples=150, deadline=None, derandomize=True)
@given(
    st.lists(
        st.sampled_from(["pause", "resume", "dispatch", "lost", "dead", "again", "done"]),
        min_size=1,
        max_size=100,
    )
)
def test_generated_lifecycle_trace(actions):
    workflow = loop_flow()
    state = Run("r", "definition", "work", "revision", paused=False)
    dispatched = set()
    for index, action in enumerate(actions):
        now = index * 100.0
        before = state
        try:
            if action in ("pause", "resume"):
                transition = control(state, action, now)
            elif action == "dispatch":
                transition = dispatch(state, workflow, now, f"attempt-{index}")
                assert transition == dispatch(state, workflow, now, f"attempt-{index}")
            elif action in ("lost", "dead"):
                transition = recover(
                    state,
                    now,
                    termination_confirmed=action == "dead",
                    reason="injected",
                    observed_revision=state.revision,
                )
            else:
                if state.active is None:
                    continue
                transition = complete(
                    state,
                    workflow,
                    Result(state.active.id, state.generation, action, "", state.revision),
                    now,
                )
        except ValueError:
            assert state == before
            continue
        state = transition.state
        if transition.effects:
            assert not before.paused and before.active is None
            assert state.active.id not in dispatched
            dispatched.add(state.active.id)
        if action == "lost" and before.active:
            assert state.active == before.active
            assert not transition.effects
        assert all(0 <= visits <= 3 for _, visits in state.visits)
        if state.status == "accepted":
            assert state.step == "finish" and state.active is None


@given(st.integers(min_value=1, max_value=100))
def test_loop_limit_is_a_hard_bound(limit):
    workflow = loop_flow(limit)
    validate(workflow)
    state = Run("r", "d", "work", "v", paused=False)
    for n in range(limit):
        state = dispatch(state, workflow, n * 2, str(n)).state
        state = complete(state, workflow, Result(str(n), n + 1, "again", "", "v"), n * 2 + 1).state
    assert dispatch(state, workflow, limit * 2, "excess").state.status == "blocked"


def test_editor_does_not_silently_rewire_other_branches():
    workflow = Workflow(
        "branch",
        "choose",
        (
            Step(
                "choose",
                "condition",
                transitions=(("true", "finish"), ("false", "finish")),
                condition_key="value",
            ),
            Step("finish", "finish"),
        ),
    )
    step = Step("required", "operation", "test", required=True)
    with pytest.raises(ValueError, match="bypasses"):
        insert_step(workflow, "choose", "true", step)
    edited = insert_step(workflow, "choose", "true", replace(step, required=False))
    assert dict(edited.step("choose").transitions)["false"] == "finish"


def test_only_current_checkpoint_is_excluded():
    evidence = (
        Artifact("units/a/handoff.md", "old", "v"),
        Artifact("units/b/handoff.md", "required", "v"),
        Artifact("product.py", "product", "v"),
    )
    assert durable_evidence(evidence, "units/a/handoff.md") == evidence[1:]
    with pytest.raises(ValueError, match="checkpoint"):
        durable_evidence(evidence[:1], "units/a/handoff.md")


def test_failed_gate_cannot_reuse_earlier_success():
    workflow = Workflow(
        "g",
        "check",
        (
            Step(
                "check",
                "check",
                "test",
                required=True,
                gate=True,
                transitions=(("passed", "check"), ("failed", "finish")),
            ),
            Step("finish", "finish"),
        ),
    )
    state = Run("r", "d", "check", "v", paused=False)
    state = dispatch(state, workflow, 0, "one").state
    state = complete(
        state, workflow, Result("one", 1, "passed", "", "v", (Artifact("proof", "hash", "v"),)), 1
    ).state
    state = dispatch(state, workflow, 2, "two").state
    state = complete(state, workflow, Result("two", 2, "failed", "", "v"), 3).state
    assert dispatch(state, workflow, 4, "finish").state.status == "blocked"
