"""Workspace commands, recovery and read-only account telemetry contracts."""

import json
import queue
import sys
from dataclasses import replace

import pytest
from sdd_core import machine
from sdd_core.models import Result, Run, Step, Workflow
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
from sdd_ui.subscriptions import read_codex_buckets, read_codex_quota
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
    assert run.paused and run.spend.calls == 0
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
    (root / "partial.txt").write_text("Unfinished edits")
    recovered = engine.recover("one", 9, True, "Executor terminated", revision(root))
    assert recovered.step == "reconcile" and recovered.status == "waiting"
    (root / "partial.txt").write_text("More unfinished edits")
    run = engine.request_recovery("one", recovered.version, 10)
    assert run.paused and run.gates == () and run.revision == revision(root)
    assert run.spend.calls == 3
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


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_an_attempt_that_changed_nothing_is_retried_or_restarted_not_reconciled(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    root = tmp_path / "project"
    root.mkdir()
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    engine.create("one", store.publish(main_flow()), root, "Requirement", revision(root), 0)
    engine.command("one", "resume", "resume", 0, 1)
    for index in range(2):
        run = engine.dispatch("one", 2 + index * 2, str(index))
        engine.complete(
            "one", Result(str(index), run.generation, "done", "Plan", run.revision), 3 + index * 2
        )
    active = engine.dispatch("one", 7, "implementation")
    # The host never started the agent: the workspace is as the attempt found it.
    lost = engine.recover("one", 8, True, "Host exited without durable completion", active.revision)
    assert lost.step == active.step and lost.status == "waiting"
    # Say an earlier build still sent it to reconciliation and the operator asks to recover.
    stuck = engine.block("one", 9, "Diagnose: nothing to repair in the product")
    with store.unit() as unit:
        unit.apply(stuck, machine.reconcile(stuck, "reconcile", stuck.revision, 10))
    parked = store.get("one")
    restarted = engine.request_recovery("one", parked.version, 11)
    assert restarted.step == active.step and restarted.paused
    assert restarted.infrastructure_failures == 0 and restarted.gates == ()
    # Once a mutating attempt changed the workspace, recovery reconciles again.
    engine.command("one", "resume", "again", restarted.version, 12)
    engine.dispatch("one", 13, "second")
    (root / "partial.txt").write_text("Unfinished edits")
    moved = engine.recover("one", 14, True, "Executor terminated", revision(root))
    assert moved.step == "reconcile"


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
    quota = read_codex_quota((sys.executable, str(script)), 500.0)
    assert quota.status == "available" and len(quota.windows) == 1
    (window,) = quota.windows
    assert (window.name, window.remaining_percent, window.resets_at) == ("session", 76, 1000)
    script.write_text("import time; time.sleep(60)")
    with pytest.raises(queue.Empty):
        read_codex_buckets((sys.executable, str(script)), timeout=0.2)


def test_subscription_failure_does_not_claim_zero_usage(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    service.config.write_text(
        json.dumps(
            {
                "schema": 1,
                "runners": {"test": {"adapter": "codex", "executable": sys.executable}},
                "profiles": {"coder": {"runner": "test", "model": "gpt-test"}},
            }
        )
    )
    monkeypatch.setattr(
        "sdd_ui.agents.read_codex_quota",
        lambda argv, now: (_ for _ in ()).throw(ValueError("offline")),
    )
    try:
        result = service.mutate("subscription", {})
        (codex,) = result["subscriptions"]
        assert result["status"] == "unavailable" and codex["windows"] == []
        assert "offline" in codex["error"], "a failed reading says so, never 0% used"
        assert service.state()["totals"]["calls"] == 0
    finally:
        service.coordinator.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retry_after_the_call_limit_grants_the_workflow_budget_once_more(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    root = tmp_path / "project"
    root.mkdir()
    flow = Workflow(
        "limited",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("failed", "work"), ("done", "finish"))),
            Step("finish", "finish"),
        ),
        max_calls=1,
    )
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    engine.create("one", store.publish(flow), root, "Task", revision(root), 0)
    engine.command("one", "resume", "resume", 0, 1)
    run = engine.dispatch("one", 2, "first")
    engine.complete("one", Result("first", run.generation, "failed", "Again", run.revision), 3)
    limited = engine.dispatch("one", 4, "second")
    assert limited.status == "blocked" and limited.reason == machine.CALL_LIMIT
    granted = engine.command("one", "retry", "grant", limited.version, 5)
    assert granted.status == "ready" and granted.spend.granted_calls == 1
    assert store.get("one").spend.granted_calls == 1, "the grant survives a reload"
    assert engine.dispatch("one", 6, "second").active is not None


def test_an_ordinary_retry_grants_no_calls():
    run = replace(Run("one", "digest", "work", "rev"), status="blocked", reason="Other")
    retried = machine.control(run, "retry", 1, call_grant=5).state
    assert retried.status == "ready" and retried.spend.granted_calls == 0
