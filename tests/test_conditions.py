"""Conditions are pure routing over the previous result, never an external effect."""

import pytest
from sdd_core.codec import canonical
from sdd_core.graph import validate
from sdd_core.machine import complete, dispatch, evaluate
from sdd_core.models import Result, Run, Step, Workflow
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace


def branching() -> Workflow:
    return Workflow(
        "branch",
        "ask",
        (
            Step("ask", "human", prompt="Mode?", transitions=(("answered", "route"),)),
            Step(
                "route",
                "condition",
                transitions=(("true", "careful"), ("false", "finish")),
                condition_key="mode.level",
                condition_value="strict",
            ),
            Step("careful", "human", prompt="Confirm", transitions=(("ok", "finish"),)),
            Step("finish", "finish"),
        ),
    )


def test_evaluate_reads_dotted_paths_and_canonical_values():
    step = branching().step("route")
    assert evaluate(step, canonical({"mode": {"level": "strict"}})) == "true"
    assert evaluate(step, canonical({"mode": {"level": "loose"}})) == "false"
    assert evaluate(step, canonical({"mode": "strict"})) == "false"
    assert evaluate(step, "{}") == "false"
    numeric = Step(
        "n",
        "condition",
        transitions=(("true", "f"), ("false", "f")),
        condition_key="count",
        condition_value="3",
    )
    assert evaluate(numeric, canonical({"count": 3})) == "true"
    assert evaluate(numeric, canonical({"count": "3"})) == "true"
    flag = Step(
        "b",
        "condition",
        transitions=(("true", "f"), ("false", "f")),
        condition_key="ok",
        condition_value="true",
    )
    assert evaluate(flag, canonical({"ok": True})) == "true"


def test_condition_routes_without_effect_and_counts_visits():
    workflow = branching()
    validate(workflow)
    state = Run("r", "d", "route", "v", paused=False)
    transition = dispatch(state, workflow, 1, "unused", canonical({"mode": {"level": "strict"}}))
    assert transition.effects == ()
    assert transition.state.step == "careful" and transition.state.active is None
    assert dict(transition.state.visits) == {"route": 1}
    assert transition.events[0].kind == "condition_evaluated"


def test_condition_respects_visit_limit():
    workflow = branching()
    state = Run("r", "d", "route", "v", paused=False, visits=(("route", 3),))
    assert dispatch(state, workflow, 1, "x").state.status == "blocked"


def test_unknown_step_is_a_value_error():
    with pytest.raises(ValueError, match="Unknown step"):
        branching().step("missing")


def test_application_routes_on_previous_human_answer(tmp_path, any_store):
    store = any_store
    definition = store.publish(branching())
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    engine.create("one", definition, tmp_path, "test", "rev", 0)
    engine.command("one", "resume", "resume", 0, 1)
    run = engine.dispatch("one", 2, "ask-1")
    facts = canonical({"mode": {"level": "strict"}})
    answer = Result("ask-1", run.generation, "answered", "strict", "rev", data=facts)
    engine.complete("one", answer, 3)
    routed = engine.dispatch("one", 4, "unused")
    assert routed.step == "careful" and routed.active is None
    with engine.store.unit() as unit:
        assert all(row.id != "unused" for row in unit.effects(("pending", "running", "done")))


def test_machine_complete_then_condition_is_deterministic():
    workflow = branching()
    state = Run("r", "d", "ask", "v", paused=False)
    state = dispatch(state, workflow, 0, "a").state
    result = Result("a", 1, "answered", "", "v", data=canonical({"mode": {"level": "x"}}))
    state = complete(state, workflow, result, 1).state
    first = dispatch(state, workflow, 2, "b", result.data)
    assert first == dispatch(state, workflow, 2, "b", result.data)
    assert first.state.step == "finish"
