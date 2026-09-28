import sys
import time
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sdd_core.codec import canonical
from sdd_core.graph import validate
from sdd_core.machine import control, dispatch
from sdd_core.models import Run, Step, Workflow
from sdd_core.portfolio import Portfolio, Ticket, ordered
from sdd_runtime.engine import Engine
from sdd_runtime.execution import ExecutionDriver
from sdd_runtime.files import revision
from sdd_runtime.process_execution import ProcessExecutionBackend
from sdd_storage.store import Store


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_time_cannot_disable_deadlines(now):
    workflow = Workflow("w", "end", (Step("end", "finish"),))
    with pytest.raises(ValueError, match="finite"):
        dispatch(Run("r", "d", "end", "v", paused=False), workflow, now, "a")


@given(st.integers(min_value=1, max_value=30))
def test_generated_dependency_dags_are_topologically_ordered(count):
    tickets = tuple(
        Ticket(f"t{i}", (f"r{i}",), tuple(f"t{j}" for j in range(i) if (i + j) % 3 == 0), "")
        for i in range(count)
    )
    portfolio = Portfolio("p", tuple(f"r{i}" for i in range(count)), tuple(reversed(tickets)))
    seen = set()
    for ticket in ordered(portfolio):
        assert set(ticket.depends_on) <= seen
        seen.add(ticket.id)
    assert len(seen) == count


@pytest.mark.parametrize("status", ["ready", "waiting", "blocked", "accepted"])
def test_pause_never_dispatches_in_any_status(status):
    workflow = Workflow(
        "w",
        "work",
        (Step("work", "operation", "test", transitions=(("done", "end"),)), Step("end", "finish")),
    )
    state = control(Run("r", "d", "work", "v", status=status, paused=False), "pause", 0).state
    with pytest.raises(ValueError):
        dispatch(state, workflow, 1, "attempt")


@pytest.mark.parametrize(
    "defect", ["target", "unreachable", "gate-bypass", "no-exit", "duplicate", "unbounded"]
)
def test_invalid_graph_matrix(defect):
    work = Step("work", "operation", "test", transitions=(("done", "check"),))
    check = Step(
        "check", "check", "test", required=True, gate=True, transitions=(("passed", "end"),)
    )
    end = Step("end", "finish")
    nodes = (work, check, end)
    if defect == "target":
        nodes = (replace(work, transitions=(("done", "missing"),)), check, end)
    elif defect == "unreachable":
        nodes += (Step("orphan", "finish"),)
    elif defect == "gate-bypass":
        nodes = (replace(work, transitions=(("done", "check"), ("skip", "end"))), check, end)
    elif defect == "no-exit":
        nodes = (replace(work, transitions=(("done", "work"),)), check, end)
    elif defect == "duplicate":
        nodes += (end,)
    else:
        nodes = (replace(work, max_visits=0), check, end)
    with pytest.raises(ValueError):
        validate(Workflow("w", "work", nodes))


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_command_uses_transport_neutral_driver(tmp_path, exit_code):
    workspace = tmp_path / "project with spaces юникод"
    workspace.mkdir()
    backend = ProcessExecutionBackend(workspace / ".sdd-engine" / "executions")
    engine = Engine(Store(tmp_path / "engine.db"))
    workflow = Workflow(
        "command",
        "check",
        (
            Step(
                "check",
                "check",
                "external",
                required=True,
                gate=True,
                transitions=(("passed", "finish"), ("failed", "finish")),
            ),
            Step("finish", "finish"),
        ),
    )
    definition = engine.store.publish(workflow)
    now = time.time()
    engine.create("r", definition, workspace, "", revision(workspace), now)
    engine.command("r", "resume", "resume", 0, now)
    engine.dispatch("r", now, "command")
    driver = ExecutionDriver(engine, backend)
    payload = canonical(
        {
            "argv": [sys.executable, "-c", f"print('proof'); raise SystemExit({exit_code})"],
            "workspace": str(workspace),
            "gate": True,
        }
    )
    try:
        handle = driver.submit("r", payload)
        assert driver.submit("r", payload) == handle
        deadline = time.monotonic() + 10
        while engine.store.get("r").active and time.monotonic() < deadline:
            driver.poll("r", time.time())
            time.sleep(0.02)
        state = engine.store.get("r")
        assert state.active is None
        assert engine.dispatch("r", time.time(), "finish").status == (
            "accepted" if exit_code == 0 else "blocked"
        )
    finally:
        backend.close()
