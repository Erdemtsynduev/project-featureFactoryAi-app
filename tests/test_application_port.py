"""The same application transactions work without SQLite. Memory is a test double."""

from dataclasses import replace

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace


@pytest.fixture
def application(any_store, tmp_path):
    store = any_store
    flow = Workflow(
        "port",
        "work",
        (
            Step("work", "operation", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace(tmp_path / "engine-work"))
    engine.create("one", definition, tmp_path, "test", "rev", 0)
    engine.command("one", "resume", "resume", 0, 1)
    return engine


def test_transaction_port_idempotency_and_conflict(application):
    engine = application
    assert engine.command("one", "resume", "resume", 0, 99).version == 1
    with pytest.raises(Conflict):
        engine.command("one", "pause", "resume", 1, 2)
    engine.dispatch("one", 2, "attempt")
    result = Result("attempt", 1, "done", "ok", "rev")
    done = engine.complete("one", result, 3)
    assert engine.complete("one", result, 99) == done
    with pytest.raises(Conflict):
        engine.complete("one", replace(result, reason="different"), 4)
    assert engine.dispatch("one", 5, "finish").status == "accepted"


def test_transaction_port_rollback_and_unknown_process(application):
    engine = application
    before = engine.store.get("one")
    with pytest.raises(RuntimeError), engine.store.unit() as unit:
        unit.save_command("temporary", canonical([]), canonical([]))
        raise RuntimeError("injected failure")
    with engine.store.unit() as unit:
        assert unit.command("temporary") is None
    assert engine.store.get("one") == before
    engine.dispatch("one", 2, "attempt")
    state = engine.recover("one", 1000, False, "process unknown", "rev")
    assert state.status == "blocked" and state.active is not None
    with pytest.raises(ValueError):
        engine.dispatch("one", 1001, "second")
