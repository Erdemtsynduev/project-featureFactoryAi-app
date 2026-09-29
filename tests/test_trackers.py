"""Trackers: work items come from them, and the factory's progress goes back in order."""

import pytest
from sdd_core.catalog import OutboxEntry
from sdd_core.models import Step, Workflow
from sdd_core.tracking import (
    TrackerReceipt,
    TrackerUpdate,
    WorkItem,
    WorkRow,
    mirror_state,
    update_json,
    update_load,
)
from sdd_factory.trackers import tracker_settings
from sdd_storage.memory import MemoryCatalog, MemoryStore
from sdd_storage.store import Store
from sdd_ui.service import WorkspaceService
from test_decomposition import complete, planning_flow


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_outbox_records_once_and_delivers_in_order(tmp_path, backend):
    catalog = (
        MemoryCatalog(MemoryStore()) if backend == "memory" else Store(tmp_path / "s.db").catalog()
    )
    first = OutboxEntry("a", "app", "run", "state", "{}")
    assert catalog.record_update(first) is True
    assert catalog.record_update(first) is False, "an id is recorded once"
    catalog.record_update(OutboxEntry("b", "app", "run", "state", '{"n":2}'))
    catalog.record_update(OutboxEntry("c", "other", "run2", "state", "{}"))
    assert [e.id for e in catalog.pending_updates("app")] == ["a", "b"]
    assert catalog.last_update("run", "state").id == "b"
    catalog.settle_update(OutboxEntry("a", "app", "run", "state", "{}", "done", 1, 0, "", "{}"))
    assert [e.id for e in catalog.pending_updates("app")] == ["b"]
    assert [e.id for e in catalog.updates("app", 5)] == ["b", "a"]
    with pytest.raises(KeyError):
        catalog.settle_update(OutboxEntry("missing", "app", "run", "state", "{}"))


def test_updates_round_trip_and_states_follow_the_board():
    update = TrackerUpdate("state", "run", "linear:1", "Why", "blocked")
    assert update_load(update_json(update)) == update
    assert mirror_state("accepted", "delivered") == "done"
    assert mirror_state("accepted", "delivering") == "working", "a parent delivers its tickets"
    assert mirror_state("running", "answer") == "needs_person"
    assert mirror_state("ready", "children_need") == "needs_person"
    assert mirror_state("blocked", "blocked") == "blocked"
    assert mirror_state("ready", "paused") == "queued"


def test_settings_never_hold_secrets():
    assert tracker_settings({"kind": "linear", "team": "ENG", "token_env": "LINEAR_API_KEY"})
    assert tracker_settings(None) == {}
    for bad in (
        {"kind": "linear", "api_key": "lin_api_123"},
        {"kind": "linear", "token": "x"},
        {"kind": "Linear!"},
        {"kind": "linear", "token_env": "not a name"},
    ):
        with pytest.raises(ValueError):
            tracker_settings(bad)


class FakeTracker:
    id = "fake"

    def __init__(self, settings):
        self.settings = settings
        self.published: list[TrackerUpdate] = []
        self.failures = 0

    def items(self, workspace):
        rows = (WorkRow("ENG-2", " ", "Pay by card", link="fake:ENG-2"),)
        return [
            WorkItem("eng-1", "Checkout", "Pay online", rows, link="fake:ENG-1", url="https://t/1")
        ]

    def publish(self, update):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("tracker is down")
        self.published.append(update)
        if update.kind == "tickets":
            return TrackerReceipt(tuple((t.run, f"fake:{t.key}") for t in update.tickets))
        return TrackerReceipt()


def test_a_tracker_item_becomes_a_feature_and_its_progress_is_mirrored(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    tracker = FakeTracker({})
    service.sources.factories = lambda: {"fake": lambda settings: tracker}
    try:
        root = tmp_path / "project"
        root.mkdir()
        service.mutate(
            "project",
            {"id": "app", "name": "App", "workspace": str(root), "tracker": {"kind": "fake"}},
        )
        planning = service.engine.store.publish(planning_flow())
        ticket = service.engine.store.publish(Workflow("ticket", "done", (Step("done", "finish"),)))
        monkeypatch.setattr(
            service.flows,
            "ensure",
            lambda name, project, language, repositories=(): (
                planning if name == "feature" else ticket
            ),
        )
        created = service.mutate("plans-sync", {"project": "app"})["created"]
        assert created == ["feature_eng-1"]
        record = service.catalog.task("feature_eng-1")
        assert record.link == "fake:ENG-1" and record.source == "https://t/1"
        with service.engine.store.unit() as unit:
            assert "Pay online" in unit.context("feature_eng-1"), (
                "the description travels in the brief"
            )

        run = service.engine.store.get("feature_eng-1")
        service.mutate("resume", {"id": run.id, "version": run.version})
        complete(service, run.id, "SPEC: pay by card", "s1")
        tickets = [
            {"id": "T1", "title": "Endpoint"},
            {"id": "T2", "title": "Screen", "depends_on": ["T1"]},
        ]
        complete(service, run.id, "Two", "t1", tickets=tickets)
        waiting = service.engine.dispatch(run.id, 12, "h1")
        service.mutate("answer", {"id": run.id, "outcome": "approved", "version": waiting.version})

        # The tracker is down: nothing is lost and the order is kept.
        tracker.failures = 1
        first = service.sync_trackers("app")
        assert first["recorded"] >= 3 and first["sent"] == 0 and tracker.published == []
        pending = service.catalog.records.pending_updates("app")
        assert pending[0].attempts == 1 and pending[0].error.startswith("ConnectionError")
        for entry in pending:
            service.catalog.records.settle_update(OutboxEntry(**{**entry.__dict__, "next_at": 0.0}))
        second = service.sync_trackers("app")
        assert second["sent"] >= 3
        kinds = [u.kind for u in tracker.published]
        assert kinds[:1] == ["state"] and "specification" in kinds and "tickets" in kinds
        mirrored = next(u for u in tracker.published if u.kind == "tickets")
        assert [(t.key, t.wave, t.depends_on) for t in mirrored.tickets] == [
            ("T1", 1, ()),
            ("T2", 2, ("T1",)),
        ]
        assert service.catalog.task(f"{run.id}-T1").link == "fake:T1"

        # Nothing changed: nothing new is recorded. A ticket's state is mirrored once linked.
        tracker.published.clear()
        third = service.sync_trackers("app")
        assert all(u.kind == "state" for u in tracker.published)
        assert {u.run for u in tracker.published} == {f"{run.id}-T1", f"{run.id}-T2"}
        assert service.sync_trackers("app") == {"recorded": 0, "sent": 0}
        assert third["sent"] == 2
    finally:
        service.coordinator.close()
