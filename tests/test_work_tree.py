"""Work is a tree: a parent's state comes from its children, at any depth."""

import sys

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Step, Workflow
from sdd_ui.attention import Attention, Progress, rollup
from sdd_ui.service import WorkspaceService

from tests.test_decomposition import complete, planning_flow

ACCEPTED = Attention("accepted", "done")
PAUSED = Attention("paused", "idle", "resume")
WORKING = Attention("working", "working")
ANSWER = Attention("answer", "attention", "answer")


def test_parent_follows_its_children_at_any_depth():
    own = {
        "feature": ACCEPTED,
        "a": ACCEPTED,
        "b": ACCEPTED,  # decomposed further
        "b1": ACCEPTED,
        "b2": WORKING,
        "c": PAUSED,
    }
    children = {"feature": ("a", "b", "c"), "b": ("b1", "b2")}
    found, progress = rollup(own, children)
    assert found["b"] == Attention("delivering", "working", detail="1/2")
    assert found["feature"] == Attention("delivering", "working", detail="1/3")
    assert progress["feature"] == Progress(1, 3) and progress["b"] == Progress(1, 2)
    assert found["a"] == ACCEPTED and "a" not in progress


@pytest.mark.parametrize(
    ("states", "code", "tone"),
    [
        ((ACCEPTED, ACCEPTED), "delivered", "done"),
        ((ACCEPTED, ANSWER), "children_need", "attention"),
        ((ACCEPTED, PAUSED), "partial", "idle"),
        ((PAUSED, PAUSED), "delivery_paused", "idle"),
        ((Attention("waiting", "waiting"), PAUSED), "delivery_waiting", "waiting"),
    ],
)
def test_parent_states(states, code, tone):
    own = {"feature": ACCEPTED, "a": states[0], "b": states[1]}
    found, _ = rollup(own, {"feature": ("a", "b")})
    assert (found["feature"].code, found["feature"].tone) == (code, tone)


def test_planning_parent_keeps_its_own_reason_and_closed_parent_is_done():
    own = {"feature": ANSWER, "a": ACCEPTED}
    found, progress = rollup(own, {"feature": ("a",)})
    assert found["feature"] == ANSWER and progress["feature"] == Progress(1, 1)
    own = {"feature": ACCEPTED, "a": ACCEPTED, "b": PAUSED}
    found, _ = rollup(own, {"feature": ("a", "b")}, frozenset({"feature"}))
    assert found["feature"] == Attention("closed", "done", detail="1/2")


def test_cyclic_records_do_not_recurse_forever():
    own = {"a": ACCEPTED, "b": ACCEPTED}
    found, _ = rollup(own, {"a": ("b",), "b": ("a",)})
    assert {found["a"].tone, found["b"].tone} <= {"done"}


def test_feature_is_delivered_by_its_tickets_or_closed_early(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "project"
        root.mkdir()
        service.mutate("project", {"id": "app", "name": "App", "workspace": str(root)})
        argv = [sys.executable, "-c", "pass"]
        ticket = service.engine.store.publish(
            Workflow(
                "ticket",
                "work",
                (
                    Step(
                        "work",
                        "check",
                        "command",
                        transitions=(("done", "done"),),
                        config=canonical({"argv": argv}),
                    ),
                    Step("done", "finish"),
                ),
            )
        )
        monkeypatch.setattr(
            service.flows, "ensure", lambda name, project, language, repositories=(): ticket
        )
        definition = service.engine.store.publish(planning_flow())
        run = service.mutate(
            "create", {"title": "Checkout", "project": "app", "definition": definition}
        )
        feature = run["id"]
        service.mutate("resume", {"id": feature, "version": run["version"]})
        complete(service, feature, "SPEC", "s1")
        tickets = [{"id": "api", "title": "API"}, {"id": "ui", "title": "UI"}]
        complete(service, feature, "Two slices", "t1", tickets=tickets)
        waiting = service.engine.dispatch(feature, 12, "h1")
        api, ui = service.mutate(
            "answer",
            {"id": feature, "outcome": "approved", "answer": "", "version": waiting.version},
        )["admitted"]
        service.engine.dispatch(feature, 13, "f1")  # the finish step

        def runs():
            return {r["id"]: r for r in service.state()["runs"]}

        # Approval ends the feature's planning, not the feature: it is not done yet.
        state = runs()[feature]
        assert state["status"] == "accepted"
        assert state["attention"]["code"] == "delivery_paused"
        assert state["lane"] == "queue"
        assert state["progress"] == {"done": 0, "total": 2}

        paused = service.engine.store.get(api)
        service.mutate("resume", {"id": api, "version": paused.version})
        complete(service, api, "done", "a1")
        service.engine.dispatch(api, 13, "a2")
        state = runs()[feature]
        assert state["attention"]["code"] == "partial"
        assert state["progress"] == {"done": 1, "total": 2}

        with pytest.raises(ValueError):
            service.mutate("close", {"id": api})
        service.mutate("close", {"id": feature})
        state = runs()[feature]
        assert state["attention"] == {
            "code": "closed",
            "tone": "done",
            "action": "",
            "detail": "1/1",
            "until": None,
        }
        assert state["lane"] == "done"
        metadata = service.state()["task_metadata"]
        assert metadata[feature]["closed"] is True
        assert "parent" not in metadata[ui] and metadata[ui]["origin"] == feature
        assert metadata[api]["parent"] == feature
        assert any(entry["kind"] == "work_closed" for entry in service.flight(run=feature))
    finally:
        service.coordinator.close()
