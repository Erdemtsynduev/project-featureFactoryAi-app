from dataclasses import replace

import pytest
from sdd_core.models import Result, Step, Workflow
from sdd_runtime.engine import Engine
from sdd_storage.store import Conflict, Store


def setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    flow = Workflow(
        "simple",
        "work",
        (
            Step("work", "operation", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    engine = Engine(store)
    run = engine.create("one", definition, workspace, "task", "rev", 0)
    engine.command(run.id, "resume", "resume", 0, 1)
    return engine, definition, workspace


def test_commands_cas_and_idempotency(tmp_path):
    engine, _, _ = setup(tmp_path)
    assert engine.command("one", "resume", "resume", 0, 99).version == 1
    with pytest.raises(Conflict):
        engine.command("one", "pause", "resume", 1, 2)
    with pytest.raises(Conflict):
        engine.command("one", "pause", "new", 0, 2)


def test_receipt_duplicates_conflicts_and_replay(tmp_path):
    engine, _, _ = setup(tmp_path)
    engine.dispatch("one", 2, "attempt")
    result = Result("attempt", 1, "done", "ok", "rev")
    state = engine.complete("one", result, 3)
    assert engine.complete("one", result, 999) == state
    with pytest.raises(Conflict):
        engine.complete("one", replace(result, reason="different"), 4)
    assert engine.store.replay("one") == state
    reopened = Engine(Store(engine.store.path))
    assert reopened.store.get("one") == state


def test_dispatch_transaction_rolls_back_on_duplicate_effect(tmp_path):
    engine, _, _ = setup(tmp_path)
    with engine.store.transaction() as db:
        db.execute(
            "INSERT INTO effects(id,run,kind,payload,status) VALUES('duplicate','other','operation','{}','done')"
        )
    before = engine.store.get("one")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        engine.dispatch("one", 2, "duplicate")
    assert engine.store.get("one") == before


def test_claim_not_released_by_expiry(tmp_path):
    engine, definition, workspace = setup(tmp_path)
    engine.create("two", definition, workspace, "task", "rev", 1)
    engine.command("two", "resume", "resume2", 0, 2)
    engine.dispatch("one", 2, "a")
    with pytest.raises(Conflict):
        engine.dispatch("two", 100000, "b")
    engine.recover("one", 100000, False, "unknown", "rev")
    with pytest.raises(Conflict):
        engine.dispatch("two", 100001, "b")


def test_dependencies_and_backups(tmp_path):
    engine, definition, workspace = setup(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    engine.create("dependent", definition, other, "task", "rev", 1, ("one",))
    engine.command("dependent", "resume", "resume2", 0, 2)
    with pytest.raises(Conflict):
        engine.dispatch("dependent", 3, "b")
    engine.dispatch("one", 2, "a")
    engine.complete("one", Result("a", 1, "done", "ok", "rev"), 3)
    engine.dispatch("one", 4, "finish")
    assert engine.dispatch("dependent", 5, "b").active
    target = tmp_path / "backup.db"
    engine.store.backup(target)
    assert Store(target).get("one").status == "accepted"
    with pytest.raises(FileExistsError):
        engine.store.backup(target)


def test_unfinished_run_retains_workspace_between_attempts(tmp_path):
    engine, definition, workspace = setup(tmp_path)
    engine.create("two", definition, workspace, "task", "rev", 1)
    engine.command("two", "resume", "resume2", 0, 2)
    engine.dispatch("one", 2, "a")
    engine.complete("one", Result("a", 1, "done", "ok", "rev"), 3)
    with pytest.raises(Conflict):
        engine.dispatch("two", 4, "b")
    engine.dispatch("one", 5, "finish")
    assert engine.dispatch("two", 6, "b").active is not None
