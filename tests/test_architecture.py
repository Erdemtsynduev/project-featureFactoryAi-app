"""Named transitions, CAS retries outside transactions, failure isolation and ports."""

import sqlite3
import sys
import time
from dataclasses import replace

import pytest
from sdd_core import machine
from sdd_core.codec import canonical
from sdd_core.models import Artifact, Result, Run, Step, Workflow
from sdd_core.ports import Conflict, StaleVersion
from sdd_core.sdk import Manifest, Packet, Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_storage.memory import MemoryCatalog, MemoryStore
from sdd_storage.store import VERSION, Store


class Project:
    def revision(self, workspace: str) -> str:
        return "rev"


class Workspace:
    """Trivial workspace whose evidence check can interleave a concurrent command."""

    def __init__(self) -> None:
        self.during_verify = lambda: None

    def resolve(self, path: str) -> str:
        return path

    def claim(self, root: str, scope: tuple[str, ...]) -> str:
        return root

    def paths(self, claim: str) -> tuple[str, ...]:
        return (claim,)

    def overlaps(self, left: str, right: str) -> bool:
        return left == right

    def normalize(self, run_id: str, result: Result, workspace: str) -> Result:
        return result

    def verify(self, result: Result, workspace: str, revision: str) -> None:
        hook, self.during_verify = self.during_verify, lambda: None
        hook()


@pytest.fixture
def engine(any_store, tmp_path):
    store = any_store
    flow = Workflow(
        "port",
        "work",
        (
            Step("work", "operation", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    application = ApplicationEngine(store, Project(), Workspace())  # type: ignore[arg-type]
    application.create("one", store.publish(flow), tmp_path, "test", "rev", 0)
    application.command("one", "resume", "resume", 0, 1)
    application.dispatch("one", 2, "attempt")
    return application


EVIDENCE = Result("attempt", 1, "done", "ok", "rev", artifacts=(Artifact("a", "0" * 64, "rev"),))


def test_result_survives_a_concurrent_command_during_evidence_checks(engine):
    engine.workspace.during_verify = lambda: engine.command(
        "one", "auto", "concurrent", engine.store.get("one").version, 3
    )
    state = engine.complete("one", EVIDENCE, 4)
    assert state.step == "finish" and state.auto_answer, "neither write may be lost"


def test_operator_answer_is_rejected_when_the_task_moved(engine):
    version = engine.store.get("one").version
    engine.workspace.during_verify = lambda: engine.command("one", "auto", "moved", version, 3)
    with pytest.raises(Conflict, match="Stale answer"):
        engine.complete("one", EVIDENCE, 4, expected=version)
    assert engine.store.get("one").active is not None


def test_stale_version_is_a_conflict(engine):
    run = engine.store.get("one")
    engine.command("one", "auto", "first", run.version, 3)
    with pytest.raises(StaleVersion), engine.store.unit() as db:
        db.apply(run, machine.block(run, 4, "late"))


def test_queue_usage_is_aggregated_by_the_backend(engine):
    with engine.store.unit() as db:
        assert db.queue_usage() == (0, 0)


def test_named_transitions_keep_rules_in_core():
    run = Run("r", "d", "work", "rev", status="blocked", paused=False, version=3, cause="blocked")
    run = replace(run, reason="earlier")
    blocked = machine.block(run, 1, "why").state
    assert (blocked.status, blocked.reason, blocked.version) == ("blocked", "why", 4)
    moved = machine.relocate(replace(run, gates=(("g", "rev"),)), "lane", 1, "/lane").state
    assert moved.revision == "lane" and moved.gates == ()
    routed = machine.reconcile(run, "inspect", "new", 1).state
    assert (routed.step, routed.status, routed.paused) == ("inspect", "ready", True)
    event = machine.guidance(run, 1, "hint").events[0]
    assert event.kind == "operator_message" and "hint" in event.detail
    with pytest.raises(ValueError):
        machine.guidance(replace(run, status="accepted"), 1, "late")
    with pytest.raises(ValueError):
        machine.block(run, 1, "")


class Exploding:
    """A handler with an unexpected defect: prepare raises a non-domain error."""

    manifest = Manifest("explode", "1", capabilities=("operation",))

    def prepare(self, packet: Packet):
        raise RuntimeError("handler defect")

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        raise AssertionError("never launched")


def test_one_failing_run_does_not_stop_the_queue(tmp_path):
    engine = Engine(Store(tmp_path / "engine.db"))
    flows = {
        "bad": Workflow(
            "bad",
            "work",
            (
                Step("work", "operation", "explode", transitions=(("done", "finish"),)),
                Step("finish", "finish"),
            ),
        ),
        "good": Workflow(
            "good",
            "check",
            (
                Step(
                    "check",
                    "check",
                    "command",
                    transitions=(("passed", "finish"),),
                    required=True,
                    gate=True,
                    config=canonical({"argv": [sys.executable, "-c", "print('ok')"]}),
                ),
                Step("finish", "finish"),
            ),
        ),
    }
    for name, flow in flows.items():
        workspace = tmp_path / name
        workspace.mkdir()
        run = engine.create(
            name, engine.store.publish(flow), workspace, name, revision(workspace), time.time()
        )
        engine.command(name, "resume", name, run.version, time.time())
    registry = Registry()
    registry.register(CommandHandler())
    registry.register(Exploding())
    coordinator = Coordinator(engine, registry)
    try:
        deadline = time.monotonic() + 20
        while engine.store.get("good").status != "accepted" and time.monotonic() < deadline:
            coordinator.tick()
            time.sleep(0.05)
        bad = engine.store.get("bad")
        assert engine.store.get("good").status == "accepted"
        assert "handler defect" in bad.reason and bad.active is None
    finally:
        coordinator.close()


def test_catalog_port_contract(tmp_path):
    sqlite = Store(tmp_path / "state.db")
    memory = MemoryStore()
    flow = Workflow("f", "finish", (Step("finish", "finish"),))
    for store in (sqlite, memory):
        store.create(Run("task", store.publish(flow), "finish", "rev"), str(tmp_path), "", "c", 0)
    for records in (sqlite.catalog(), MemoryCatalog(memory)):
        records.save_project("p", '{"id":"p"}')
        records.save_plans("p", (("b", "2"), ("a", "1")))
        records.save_task("task", "first")
        records.save_task("task", "second")
        records.save_preference("discovery", "[]")
        assert records.projects() == ('{"id":"p"}',)
        assert records.plans() == (("p", "1"), ("p", "2"))
        assert records.tasks() == (("task", "first"),)
        assert records.preference("discovery") == "[]" and records.preference("x") is None
        assert records.agent_calls() == () and records.daily_dispatches(14) == ()


def test_schema_upgrade_adopts_ui_tables_with_backup(tmp_path):
    path = tmp_path / "state.db"
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET version=2")
        db.execute("INSERT INTO ui_projects VALUES('p','{}')")
    store = Store(path)
    assert list(tmp_path.glob("*.pre-v3-*.bak"))
    assert store.catalog().projects() == ("{}",)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM meta").fetchone()[0] == VERSION
