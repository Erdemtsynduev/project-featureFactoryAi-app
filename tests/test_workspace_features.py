"""Workspace commands, recovery and read-only account telemetry contracts."""

import json
import queue
import sys
from dataclasses import replace

import pytest
from sdd_core import machine
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.files import revision
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_storage.memory import MemoryStore
from sdd_storage.store import Store
from sdd_ui.service import WorkspaceService
from sdd_ui.subscriptions import read_codex_limits
from sdd_workflows.templates import main_flow


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_message_persists_once_and_enters_next_packet(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    root = tmp_path / "project"
    root.mkdir()
    flow = Workflow(
        "messages",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    engine.create("one", store.publish(flow), root, "Original", revision(root), 0)
    run = engine.message("one", "Ответы на русском", "message", 0, 1)
    assert run.paused and run.calls == 0
    assert engine.message("one", "Ответы на русском", "message", 0, 2) == run
    with pytest.raises(Conflict):
        engine.message("one", "Stale", "other", 0, 2)
    with store.unit() as unit:
        assert unit.context("one").count("Ответы на русском") == 1
    engine.command("one", "resume", "resume", run.version, 3)
    engine.dispatch("one", 4, "attempt")
    coordinator = Coordinator(engine, Registry())
    packet = coordinator.packet("one")
    assert "Ответы на русском" in packet.context
    assert packet.attempt.id == "attempt"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_recovery_reconciles_changed_revision_without_bypassing_gates(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    root = tmp_path / "project"
    root.mkdir()
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    flow = main_flow()
    engine.create("one", store.publish(flow), root, "Requirement", revision(root), 0)
    engine.command("one", "resume", "resume", 0, 1)
    for index in range(2):
        run = engine.dispatch("one", 2 + index * 2, str(index))
        engine.complete(
            "one", Result(str(index), run.generation, "done", "Plan", run.revision), 3 + index * 2
        )
    active = engine.dispatch("one", 7, "implementation")
    with pytest.raises(ValueError, match="inactive"):
        engine.request_recovery("one", active.version, 8)
    recovered = engine.recover("one", 9, True, "Executor terminated", active.revision)
    assert recovered.step == "reconcile" and recovered.status == "waiting"
    (root / "partial.txt").write_text("Unfinished edits")
    run = engine.request_recovery("one", recovered.version, 10)
    assert run.paused and run.gates == () and run.revision == revision(root)
    assert run.calls == 3
    engine.command("one", "resume", "again", run.version, 11)
    current = engine.dispatch("one", 12, "reconciliation")
    checked = engine.complete(
        "one", Result("reconciliation", current.generation, "done", "Inspected", run.revision), 13
    )
    assert checked.step == "checks" and checked.status != "accepted"
    # Even a corrupted caller cannot accept a run without its required gates.
    assert (
        machine.dispatch(replace(checked, step="accepted"), flow, 14, "finish").state.status
        == "blocked"
    )


def test_project_registration_language_and_demo_are_isolated(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        flow = Workflow("empty", "finish", (Step("finish", "finish"),))
        definition = service.engine.store.publish(flow)
        for name, language in (("first", "ru"), ("second", "en")):
            root = tmp_path / name
            root.mkdir()
            service.mutate(
                "project", {"id": name, "name": name, "workspace": str(root), "language": language}
            )
            service.mutate("create", {"id": name, "project": name, "definition": definition})
        assert len(service.state()["projects"]) == 2
        assert "Response language: Russian" in service.detail("first")["context"]
        assert "Response language: English" in service.detail("second")["context"]
        assert all(run["paused"] for run in service.state()["runs"])
        for _ in range(2):
            demo = service.mutate("interactive-demo", {"language": "en"})
            assert demo["active"] and demo["calls"] == 0
        assert service.state()["settings"]["running"] is False
        assert service.state()["totals"]["calls"] == 0
    finally:
        service.coordinator.close()


def test_codex_account_probe_handshake_no_model_and_null_limits(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("""import json, sys
init = json.loads(sys.stdin.readline())
assert init["method"] == "initialize"
print(json.dumps({"id": 0, "result": {}}), flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
assert json.loads(sys.stdin.readline())["method"] == "account/rateLimits/read"
print(json.dumps({"id": 1, "result": {"rateLimitsByLimitId": {
    "codex": {"primary": {"usedPercent": 24, "windowDurationMins": 300, "resetsAt": 1000}, "secondary": None}
}}}), flush=True)
""")
    result = read_codex_limits((sys.executable, str(script)))
    assert result["status"] == "available"
    assert result["windows"][0]["remaining_percent"] == 76
    assert len(result["windows"]) == 1
    script.write_text("import time; time.sleep(60)")
    with pytest.raises(queue.Empty):
        read_codex_limits((sys.executable, str(script)), timeout=0.2)


def test_subscription_failure_does_not_claim_zero_usage(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    service.config.write_text(
        json.dumps(
            {
                "schema": 1,
                "runners": {"test": {"adapter": "codex", "executable": sys.executable}},
                "profiles": {},
            }
        )
    )
    monkeypatch.setattr(
        "sdd_ui.agents.read_codex_limits",
        lambda argv: (_ for _ in ()).throw(ValueError("offline")),
    )
    try:
        result = service.mutate("subscription", {})
        assert result["status"] == "unavailable" and result["windows"] == []
        assert service.state()["totals"]["calls"] == 0
    finally:
        service.coordinator.close()
