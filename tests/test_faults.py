import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sdd_core.codec import canonical, result_json
from sdd_core.context import ContextRecord, assemble
from sdd_core.models import Result
from sdd_runtime.files import atomic_write, revision
from sdd_storage.store import Store
from test_runtime import hosted, runtime, settle
from test_storage import setup


def test_result_written_before_ack_recovered_without_reexecution(tmp_path):
    coordinator = runtime(tmp_path)
    coordinator.tick()
    live = hosted(coordinator)[0]
    live.process.wait(timeout=10)
    packet = coordinator.packets[live.request.id]
    result = coordinator.registry.get("command").collect(
        packet, 0, revision(Path(packet.workspace))
    )
    atomic_write(Path(packet.directory) / "receipt.json", result_json(result))
    # The coordinator loses its memory of the host, as a crash would.
    live.sandbox.close()
    coordinator.supervisor.live.clear()
    coordinator.restore(time.time())
    state = coordinator.engine.store.get("one")
    assert state.active is None and state.step == "finish"
    assert settle(coordinator).status == "accepted"
    with coordinator.engine.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1


def test_host_completed_before_coordinator_collected(tmp_path):
    coordinator = runtime(tmp_path)
    coordinator.tick()
    live = hosted(coordinator)[0]
    live.process.wait(timeout=10)
    live.sandbox.close()
    coordinator.supervisor.live.clear()
    coordinator.restore(time.time())
    assert settle(coordinator).status == "accepted"


def test_disk_full_rolls_back_result_and_keeps_ownership(tmp_path):
    engine, _, _ = setup(tmp_path)
    engine.dispatch("one", 2, "attempt")
    with engine.store.transaction() as db:
        db.execute(
            "CREATE TRIGGER inject_disk_full BEFORE INSERT ON results BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
    before = engine.store.get("one")
    with pytest.raises(sqlite3.DatabaseError, match="disk is full"):
        engine.complete("one", Result("attempt", 1, "done", "ok", "rev"), 3)
    assert engine.store.get("one") == before
    with engine.store.transaction() as db:
        assert (
            db.execute("SELECT status FROM effects WHERE id='attempt'").fetchone()[0] == "pending"
        )


def test_modified_evidence_cannot_accept(tmp_path):
    coordinator = runtime(tmp_path)
    coordinator.tick()
    live = hosted(coordinator)[0]
    live.process.wait(timeout=10)
    coordinator.collect(time.time())
    (Path(live.plan.folder) / "stdout.log").write_text("changed")
    coordinator.tick()
    assert coordinator.engine.store.get("one").status != "accepted"


def test_required_context_never_silently_truncated():
    records = (
        ContextRecord("requirements", "spec", "accept everything", "", "approved", required=True),
        ContextRecord("old", "checkpoint", "stale", "old-revision", "attempt", priority=5),
    )
    content, excluded = assemble(records, "new-revision", 200)
    assert "accept everything" in content and excluded == ("old",)
    with pytest.raises(ValueError, match="Required"):
        assemble(records, "new-revision", 5)


def test_v1_migration_backs_up_and_preserves_state(tmp_path):
    engine, _, _ = setup(tmp_path)
    before = engine.store.get("one")
    with engine.store.transaction() as db:
        db.execute("DROP TABLE bindings")
        db.execute("DROP TABLE project_policies")
        db.execute("UPDATE meta SET version=1")
    reopened = Store(engine.store.path)
    assert reopened.get("one") == before
    assert list(tmp_path.glob("*.pre-v2-*.bak"))


def test_model_profile_is_pinned(tmp_path):
    coordinator = runtime(tmp_path)
    coordinator.bind("one")
    handler = coordinator.registry.get("command")
    old = handler.manifest
    try:
        # Until an attempt is dispatched the pin follows the installed handler.
        handler.manifest = replace(old, version="98.0")
        coordinator.bind("one")
        coordinator.engine.dispatch("one", time.time(), "attempt")
        handler.manifest = replace(old, version="99.0")
        with pytest.raises(ValueError, match="Pinned"):
            coordinator.bind("one")
    finally:
        handler.manifest = old


def test_policy_cannot_be_removed_by_graph(tmp_path):
    engine, definition, workspace = setup(tmp_path)
    with engine.store.transaction() as db:
        db.execute(
            "INSERT INTO project_policies VALUES(?,?)",
            (str(workspace.resolve()), canonical(["checks", "review"])),
        )
    with pytest.raises(ValueError, match="Policy"):
        engine.create("another", definition, workspace, "task", "rev", 1)


def test_identifier_cannot_escape_artifact_root(tmp_path):
    engine, definition, workspace = setup(tmp_path)
    with pytest.raises(ValueError, match="run id"):
        engine.create("../outside", definition, workspace, "task", "rev", 1)
