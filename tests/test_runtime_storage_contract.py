"""Identical runtime scenarios against independent SQLite and in-memory backends."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sdd_core.codec import canonical
from sdd_core.execution import ExecutionHandle, ExecutionObservation
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.execution import ExecutionDriver
from sdd_runtime.files import revision
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_storage.memory import MemoryStore
from sdd_storage.store import Store


@pytest.fixture(params=["sqlite", "memory"])
def configured(request, tmp_path):
    store = Store(tmp_path / "state.db") if request.param == "sqlite" else MemoryStore()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    flow = Workflow(
        "contract",
        "work",
        (
            Step(
                "work",
                "check",
                "command",
                transitions=(("passed", "finish"),),
                required=True,
                gate=True,
                config=canonical({"argv": [sys.executable, "-c", "print('contract')"]}),
            ),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    engine.create("one", definition, workspace, "test", revision(workspace), 0)
    engine.command("one", "resume", "resume", 0, 1)
    registry = Registry()
    registry.register(CommandHandler())
    return engine, registry, workspace


def test_real_process_acceptance_with_replaced_store(configured, tmp_path):
    engine, registry, _ = configured
    health = tmp_path / "health.json"
    coordinator = Coordinator(engine, registry, health)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            coordinator.tick()
            state = engine.store.get("one")
            if state.status in ("accepted", "blocked"):
                break
            time.sleep(0.05)
        assert state.status == "accepted", state.reason
        assert state.calls == 0 and health.exists()
        with engine.store.unit() as unit:
            assert unit.effects(("done",)) and not unit.effects(("running", "pending"))
    finally:
        coordinator.close()


def test_atomic_ownership_arbitration_and_rollback(configured):
    engine, _, _ = configured
    engine.dispatch("one", 2, "attempt")
    with pytest.raises(RuntimeError), engine.store.unit() as unit:
        unit.claim_host("attempt", "{}", "discard")
        raise RuntimeError("crash before commit")
    with engine.store.unit() as unit:
        assert unit.effect("attempt").host_nonce is None

    def claim(local):
        try:
            with engine.store.unit() as unit:
                if local:
                    unit.claim_host("attempt", "{}", "nonce")
                else:
                    unit.bind_execution("one", "attempt", "external", "{}")
            return "claimed"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (True, False))) == ["claimed", "conflict"]


@pytest.mark.parametrize("external_first", [True, False])
def test_dispatch_ownership_cannot_be_transferred(configured, external_first):
    engine, _, _ = configured
    engine.dispatch("one", 2, "attempt")
    with engine.store.unit() as unit:
        if external_first:
            unit.bind_execution("one", "attempt", "backend", "request")
            unit.bind_execution("one", "attempt", "backend", "request")
            with pytest.raises(Conflict):
                unit.claim_host("attempt", "packet", "nonce")
            with pytest.raises(Conflict):
                unit.bind_execution("one", "attempt", "other", "request")
        else:
            unit.claim_host("attempt", "packet", "nonce")
            with pytest.raises(Conflict):
                unit.claim_host("attempt", "packet", "second")
            with pytest.raises(Conflict):
                unit.host_started("attempt", "wrong", 10, 3)
            unit.host_started("attempt", "nonce", 10, 3)
            with pytest.raises(Conflict):
                unit.bind_execution("one", "attempt", "backend", "request")


def test_restore_durable_exit_without_relaunch(configured):
    engine, registry, _ = configured
    engine.dispatch("one", 2, "attempt")
    first = Coordinator(engine, registry)
    try:
        packet = first._packet("one")
        first.bind("one")
        with engine.store.unit() as unit:
            unit.claim_host("attempt", "{}", "nonce")
        Path(packet.directory, "stdout.log").write_text("passed", encoding="utf-8")
        Path(packet.directory, "stderr.log").write_text("", encoding="utf-8")
        Path(packet.directory, "exit.json").write_text(
            canonical({"exit_code": 0, "completed_at": 3, "nonce": "nonce"}), encoding="utf-8"
        )
    finally:
        first.close()
    reopened = Coordinator(engine, registry)
    try:
        reopened.restore(5)
        assert not reopened.live
        assert engine.store.get("one").step == "finish"
        reopened.restore(6)
        assert engine.dispatch("one", 7, "finish").status == "accepted"
    finally:
        reopened.close()


def test_binding_portfolio_policy_and_projection_transactions(configured):
    engine, _, workspace = configured
    with engine.store.unit() as unit:
        unit.bind_handler("one", "handler", "version1")
        unit.bind_portfolio("portfolio", "approved")
        unit.set_policy(str(workspace), ("review", "check", "review"))
        assert unit.context("one") == "test"
        assert unit.runnable() == ("one",)
        assert unit.last_transition() == 1
    with pytest.raises(Conflict), engine.store.unit() as unit:
        unit.set_policy(str(workspace), ())
        unit.bind_handler("one", "handler", "version2")
    with engine.store.unit() as unit:
        assert unit.policy(str(workspace)) == ("check", "review")
        assert unit.portfolio("portfolio") == "approved"
        with pytest.raises(Conflict):
            unit.bind_portfolio("portfolio", "changed")


class External:
    id = "external"

    def __init__(self):
        self.requests = {}
        self.observation = None

    def start(self, request):
        self.requests[request.id] = request
        return ExecutionHandle(self.id, request.id, request.generation)

    def reconcile(self, handle):
        return self.observation or ExecutionObservation(handle, "unknown")

    def cancel(self, handle):
        pass


def test_external_driver_unknown_then_confirmed_termination(configured):
    engine, registry, _ = configured
    engine.dispatch("one", 2, "attempt")
    external = External()
    driver = ExecutionDriver(engine, external)
    handle = driver.submit("one", "payload")
    assert driver.submit("one", "payload") == handle
    coordinator = Coordinator(engine, registry)
    try:
        before = engine.store.get("one")
        coordinator.restore(5)
        assert engine.store.get("one") == before
        with pytest.raises(Conflict):
            coordinator.start("one")
    finally:
        coordinator.close()
    driver.poll("one", 5)
    assert engine.store.get("one").active is not None
    external.observation = ExecutionObservation(handle, "terminated")
    ExecutionDriver(engine, external).poll("one", 6)
    assert engine.store.get("one").active is None
    assert len(external.requests) == 1


def test_result_projection_order_and_idempotency(configured):
    engine, _, _ = configured
    engine.dispatch("one", 2, "attempt")
    # A blocked check is valid and records a result without accepting the run.
    result = Result("attempt", 1, "blocked", "test", engine.store.get("one").revision)
    engine.complete("one", result, 3)
    engine.complete("one", result, 4)
    with engine.store.unit() as unit:
        assert len(unit.recent_results("one", 5)) == 1
        assert unit.results("one") == (result,)
    with pytest.raises(Conflict):
        engine.complete("one", replace(result, reason="changed"), 5)
