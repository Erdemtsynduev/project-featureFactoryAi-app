"""A handler is pinned per attempt: upgrades apply between attempts and never block a run."""

import json
import time
from dataclasses import replace

import pytest
from sdd_core.models import Attempt, Effect, Run, Step, Transition, Workflow
from sdd_core.ports import Conflict
from sdd_core.records import HANDLER_CHANGED_WHILE_LIVE
from sdd_ui.service import LEGACY_BLOCKS, WorkspaceService
from test_runtime import runtime, settle


def test_the_pin_follows_the_handler_between_attempts_only(tmp_path, any_store):
    store = any_store
    flow = Workflow(
        "w",
        "work",
        (Step("work", "operation", "x", transitions=(("done", "end"),)), Step("end", "finish")),
    )
    store.create(
        Run("r", store.publish(flow), "work", "v", paused=False),
        str(tmp_path),
        "",
        str(tmp_path),
        0,
    )
    with store.unit() as db:
        db.bind_handler("r", "x", "v1")
        db.bind_handler("r", "x", "v2")  # nothing ran yet
        run = db.run("r")
        attempt = Attempt("a1", "work", 1, 0, 60, "v")
        live = replace(run, status="running", active=attempt, generation=1, version=run.version + 1)
        db.apply(run, Transition(live, (), (Effect("a1", "operation", attempt),)))
    with store.unit() as db, pytest.raises(Conflict, match=HANDLER_CHANGED_WHILE_LIVE):
        db.bind_handler("r", "x", "v3")
    with store.unit() as db:
        db.effect_status("a1", "done")
        db.bind_handler("r", "x", "v3")  # the attempt ended: the next one runs v3


def test_an_attempt_records_the_handler_it_ran_with(tmp_path):
    coordinator = runtime(tmp_path)
    try:
        assert coordinator.tick(time.time()) == 1
        (folder,) = [
            p for p in (tmp_path / "workspace" / ".sdd-engine" / "one").iterdir() if p.is_dir()
        ]
        handler = json.loads((folder / "handler.json").read_text(encoding="utf-8"))
        assert handler["id"] == "command"
        settle(coordinator)
    finally:
        coordinator.close()


def test_a_handler_change_under_a_live_attempt_waits_instead_of_blocking(tmp_path):
    coordinator = runtime(tmp_path, code="import time; time.sleep(1)")
    try:
        coordinator.engine.dispatch("one", time.time(), "live")  # live, not yet submitted here
        handler = coordinator.registry.get("command")
        handler.manifest = replace(handler.manifest, version="99.0")
        assert coordinator._advance("one", time.time()) is False
        assert coordinator.engine.store.get("one").status == "running", "never blocked"
    finally:
        coordinator.close()


def test_runs_an_earlier_engine_blocked_by_its_pin_are_released_once(tmp_path):
    database = tmp_path / "ui.db"
    service = WorkspaceService(database)
    root = tmp_path / "project"
    root.mkdir()
    flow = Workflow(
        "w", "work", (Step("work", "human", transitions=(("done", "end"),)), Step("end", "finish"))
    )
    definition = service.engine.store.publish(flow)
    run = service.engine.create("stuck", definition, root, "", "rev", 0)
    reason = "Pinned handler settings or version changed"
    assert reason in LEGACY_BLOCKS
    service.engine.block("stuck", 1, reason)
    service.coordinator.close()
    reopened = WorkspaceService(database)
    try:
        state = reopened.engine.store.get("stuck")
        assert state.status == "ready" and state.version > run.version
        assert any(e.get("legacy") for e in reopened.flight(run="stuck"))
    finally:
        reopened.coordinator.close()
