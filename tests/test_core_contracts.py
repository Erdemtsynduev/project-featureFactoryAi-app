"""Declarative codec, run invariants and the pure admission policy of the core."""

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sdd_core import machine
from sdd_core.admission import QueueBudget, Slots
from sdd_core.codec import (
    canonical,
    object_json,
    result_json,
    result_load,
    run_json,
    run_load,
    workflow_json,
    workflow_load,
)
from sdd_core.graph import dependency_layers
from sdd_core.models import (
    CAUSES,
    KINDS,
    STATUSES,
    Artifact,
    Attempt,
    Result,
    Run,
    Spend,
    Step,
    Usage,
    Workflow,
)
from sdd_core.schema import workflow_schema
from sdd_workflows.templates import main_flow

texts = st.text(alphabet="abc_-01", min_size=1, max_size=8)
times = st.floats(min_value=0, max_value=1e9, allow_nan=False)
counts = st.integers(min_value=0, max_value=1000)
attempts = st.builds(Attempt, texts, texts, counts, times, times, texts, st.none() | texts)
runs = st.builds(
    Run,
    id=texts,
    workflow_digest=texts,
    step=texts,
    revision=texts,
    status=st.sampled_from(STATUSES),
    paused=st.booleans(),
    version=counts,
    generation=counts,
    active=st.none() | attempts,
    visits=st.lists(st.tuples(texts, counts), max_size=3).map(tuple),
    completed=st.lists(texts, max_size=3).map(tuple),
    gates=st.lists(st.tuples(texts, texts), max_size=3).map(tuple),
    spend=st.builds(Spend, counts, counts, counts, counts, st.booleans()),
    cause=st.sampled_from(CAUSES),
    wake_at=st.none() | times,
    reason=st.text(max_size=20),
    previous_attempt=st.none() | texts,
)
optional = st.none() | counts
results = st.builds(
    Result,
    texts,
    counts,
    texts,
    st.text(max_size=20),
    texts,
    st.lists(st.builds(Artifact, texts, texts, texts), max_size=2).map(tuple),
    st.builds(Usage, optional, optional, optional, optional),
    st.none() | st.booleans(),
    st.none() | st.booleans(),
    st.none() | times,
)


@given(runs)
def test_run_documents_round_trip(run):
    assert run_load(run_json(run)) == run


@given(results)
def test_result_documents_round_trip(result):
    assert result_load(result_json(result)) == result


def test_workflow_documents_round_trip():
    flow = main_flow()
    assert workflow_load(workflow_json(flow)) == flow


def test_codec_applies_declared_defaults_and_refuses_unknown_fields():
    loaded = workflow_load('{"id":"w","entry":"a","steps":[{"id":"a","kind":"finish"}]}')
    assert loaded.max_calls == 10 and loaded.steps[0].max_visits == 3
    with pytest.raises(ValueError, match="Unknown workflow fields"):
        workflow_load('{"id":"w","entry":"a","steps":[],"extra":1}')
    with pytest.raises(ValueError, match="Unknown step fields"):
        workflow_load('{"id":"w","entry":"a","steps":[{"id":"a","kind":"finish","x":1}]}')
    with pytest.raises(ValueError, match="Unknown kind"):
        workflow_load('{"id":"w","entry":"a","steps":[{"id":"a","kind":"robot"}]}')
    with pytest.raises(ValueError, match="2 elements"):
        workflow_load(
            '{"id":"w","entry":"a","steps":[{"id":"a","kind":"check","transitions":[["x"]]}]}'
        )
    with pytest.raises(ValueError, match="Unknown result fields"):
        result_load(canonical({**object_json(result_json(Result("a", 1, "x", "", "r"))), "y": 1}))
    with pytest.raises(ValueError, match="needs revision"):
        run_load('{"id":"r","workflow_digest":"d","step":"s"}')
    with pytest.raises(ValueError, match="must be integer"):
        run_load('{"id":"r","workflow_digest":"d","step":"s","revision":"v","calls":true}')


def test_schema_uses_the_model_vocabulary():
    step = workflow_schema()["properties"]["steps"]["items"]["properties"]  # type: ignore[index]
    assert step["kind"]["enum"] == list(KINDS)  # type: ignore[index]


def test_transitions_refuse_a_status_that_contradicts_the_live_attempt():
    attempt = Attempt("a1", "work", 1, 0.0, 10.0, "r")
    running = Run("r", "d", "work", "r", status="running", paused=False, active=attempt)
    with pytest.raises(ValueError, match="cannot hold a live attempt"):
        machine.guidance(replace(running, status="ready"), 1, "hi")
    with pytest.raises(ValueError, match="needs its attempt"):
        machine.guidance(replace(running, active=None), 1, "hi")
    # An uncertain owner keeps the attempt while blocked.
    assert (
        machine.recover(
            running, 1, termination_confirmed=False, reason="lost", observed_revision="r"
        ).state.active
        == attempt
    )


def test_unknown_operator_command_is_refused():
    with pytest.raises(ValueError, match="Unsupported command"):
        machine.control(Run("r", "d", "s", "v"), "explode", 1)


def test_slots_count_agents_and_operations_separately():
    slots = Slots(agents=2, operations=1)
    assert slots.free("agent", ["agent", "check"])
    assert not slots.free("agent", ["agent", "agent"])
    assert not slots.free("operation", ["check"])
    assert slots.free("check", ["agent", "agent"])
    with pytest.raises(ValueError, match="At least one agent"):
        Slots(agents=0)


def test_queue_budget_limits_calls_and_planning_calls():
    assert not QueueBudget().exhausted(10**6, 10**6, True)
    assert QueueBudget(calls=3).exhausted(3, 0, False)
    assert not QueueBudget(planning_calls=1).exhausted(5, 1, False)
    assert QueueBudget(planning_calls=1).exhausted(5, 1, True)
    with pytest.raises(ValueError, match="cannot be negative"):
        QueueBudget(calls=-1)


def test_dependency_layers_keep_declaration_order_and_refuse_cycles():
    assert dependency_layers({"b": (), "a": (), "c": ("a", "b")}, "cycle") == (("b", "a"), ("c",))
    with pytest.raises(ValueError, match="cycle"):
        dependency_layers({"a": ("b",), "b": ("a",)}, "cycle")
    with pytest.raises(ValueError, match="cycle"):
        dependency_layers({"a": ("missing",)}, "cycle")


def test_step_options_and_routes_are_read_from_the_step():
    step = Step(
        "plan", "agent", "h", transitions=(("done", "end"),), config='{"purpose":"planning"}'
    )
    assert step.options.planning
    assert step.target("done") == "end" and step.target("other") is None
    assert step.options.auto_answer_outcome(step.transitions) == "done"


def test_run_wire_stays_flat_for_storage_queries():
    stored = object_json(run_json(Run("r", "d", "s", "v", spend=Spend(calls=2, tokens=7))))
    assert stored["calls"] == 2 and stored["tokens"] == 7 and "spend" not in stored


@pytest.mark.parametrize(
    ("status", "reason", "cause"),
    [
        ("blocked", machine.WAIT_RETRY_LIMIT, "wait_limit"),
        ("blocked", machine.CALL_LIMIT, "call_limit"),
        ("blocked", "Process ownership uncertain: host alive", "uncertain"),
        ("blocked", "Unsatisfied gates: ['review']", "acceptance"),
        ("blocked", "Provider protocol: bad JSON", "blocked"),
        ("ready", machine.STOP_REQUESTED, "stop"),
        ("waiting", "Rate limited", ""),
    ],
)
def test_runs_stored_before_causes_get_them_once_when_the_store_opens(
    tmp_path, status, reason, cause
):
    import sqlite3

    from sdd_storage.store import Store

    path = tmp_path / "old.db"
    Store(path).publish(Workflow("w", "s", (Step("s", "finish"),)))
    old = object_json(run_json(Run("r", "d", "s", "v", status=status, reason=reason)))
    del old["cause"]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runs VALUES('r',?,0,'w','','w',0)", (canonical(old),))
        db.execute("UPDATE meta SET version=5")
    store = Store(path)  # a version 5 database: backed up, then rewritten once
    assert store.get("r").cause == cause
    assert list(tmp_path.glob("old.db.pre-v6-*.bak")), "backed up before the rewrite"
    with sqlite3.connect(path) as db:
        assert "cause" in object_json(db.execute("SELECT state FROM runs").fetchone()[0])


def test_a_feature_stored_as_a_requirement_is_rewritten_once(tmp_path):
    import sqlite3

    from sdd_storage.store import Store

    path = tmp_path / "old.db"
    store = Store(path)
    store.publish(Workflow("w", "s", (Step("s", "finish"),)))
    store.create(
        Run("r", store.publish(Workflow("w", "s", (Step("s", "finish"),))), "s", "v"),
        "w",
        "",
        "w",
        0,
    )
    store.catalog().save_task("r", '{"kind":"requirement","project":"p"}')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET version=5")
    assert object_json(Store(path).catalog().tasks()[0][1])["kind"] == "feature"


def test_rewording_a_reason_does_not_change_behaviour():
    limited = machine.block(Run("r", "d", "s", "v"), 1, "Calls used up", cause="call_limit")
    granted = machine.control(limited.state, "retry", 2, call_grant=3).state
    assert granted.spend.granted_calls == 3 and granted.cause == "" and granted.reason == ""


def test_accepted_is_final_in_the_status_table():
    accepted = Run("r", "d", "s", "v", status="accepted")
    with pytest.raises(ValueError, match="accepted task cannot become blocked"):
        machine.block(accepted, 1, "late failure")
    with pytest.raises(ValueError, match="rule's cause"):
        machine.block(Run("r", "d", "s", "v"), 1, "why", cause="stop")


def test_resume_withdraws_a_stop_but_a_block_keeps_its_cause():
    idle = Run("r", "d", "s", "v", paused=False)
    stopped = machine.control(idle, "stop", 1).state
    assert machine.stop_requested(stopped)
    resumed = machine.control(stopped, "resume", 2).state
    assert (resumed.cause, resumed.reason, resumed.paused) == ("", "", False)
    blocked = machine.block(idle, 1, "Provider protocol: bad JSON").state
    kept = machine.control(machine.control(blocked, "stop", 2).state, "resume", 3).state
    assert kept.status == "blocked" and kept.cause == "stop" and kept.reason


def test_an_accepted_task_has_nothing_to_stop():
    with pytest.raises(ValueError, match="nothing to stop"):
        machine.control(Run("r", "d", "s", "v", status="accepted"), "stop", 1)
