"""Approving a requirement admits its tickets as paused dependent child tasks."""

import json
import sys
import time

from sdd_core.codec import canonical
from sdd_core.machine import WAIT_RETRY_LIMIT
from sdd_core.models import Result, Step, Workflow
from sdd_factory.model import TaskRecord
from sdd_ui.queue import REVIVE_AFTER
from sdd_ui.service import WorkspaceService


def planning_flow() -> Workflow:
    # A check stands in for the planning agent: the test supplies its result.
    argv = [sys.executable, "-c", "pass"]
    spec = canonical({"argv": argv, "produces": "specification"})
    tickets = canonical({"argv": argv, "produces": "tickets"})
    return Workflow(
        "feature",
        "spec",
        (
            Step("spec", "check", "command", transitions=(("done", "tickets"),), config=spec),
            Step("tickets", "check", "command", transitions=(("done", "approve"),), config=tickets),
            Step(
                "approve",
                "human",
                prompt="Approve",
                transitions=(("approved", "accepted"), ("rework", "spec")),
            ),
            Step("accepted", "finish"),
        ),
    )


def complete(service: WorkspaceService, run_id: str, reason: str, attempt: str, **data):
    engine = service.engine
    run = engine.dispatch(run_id, 10, attempt)
    return engine.complete(
        run_id,
        Result(attempt, run.generation, "done", reason, run.revision, data=canonical(data)),
        11,
    )


def test_approval_creates_ticket_tasks_once_with_dependencies(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "project"
        root.mkdir()
        service.mutate("project", {"id": "app", "name": "App", "workspace": str(root)})
        ticket = service.engine.store.publish(Workflow("ticket", "done", (Step("done", "finish"),)))
        monkeypatch.setattr(
            service.flows, "ensure", lambda name, project, language, repositories=(): ticket
        )
        definition = service.engine.store.publish(planning_flow())
        run = service.mutate(
            "create",
            {"title": "Checkout", "project": "app", "definition": definition, "context": "Pay"},
        )
        identifier = run["id"]
        assert identifier.startswith("checkout-")
        service.mutate("resume", {"id": identifier, "version": run["version"]})
        complete(service, identifier, "SPEC: pay by card", "s1")
        tickets = [
            {"id": "ui", "title": "Pay screen", "depends_on": ["api"], "acceptance": ["AC-1"]},
            {"id": "api", "title": "Pay endpoint", "paths": ["api/"]},
        ]
        complete(service, identifier, "Two slices", "t1", tickets=tickets)
        waiting = service.engine.dispatch(identifier, 12, "h1")
        detail = service.detail(identifier)
        assert [t["id"] for t in detail["tickets"]] == ["api", "ui"]

        answered = service.mutate(
            "answer",
            {"id": identifier, "outcome": "approved", "answer": "", "version": waiting.version},
        )
        children = answered["admitted"]
        assert children == [identifier + "-api", identifier + "-ui"]
        state = service.state()
        metadata = state["task_metadata"]
        assert metadata[children[1]] == {
            "kind": "ticket",
            "language": "ru",
            "parent": identifier,
            "project": "app",
            "title": "Pay screen",
        }
        runs = {r["id"]: r for r in state["runs"]}
        assert all(runs[c]["paused"] for c in children)
        with service.engine.store.unit() as unit:
            assert [d.id for d in unit.dependencies(children[1])] == [children[0]]
            context = unit.context(children[1])
        assert "Ticket ui: Pay screen" in context and "SPEC: pay by card" in context
        assert runs[children[1]]["attention"]["code"] == "paused"
        log = service.flight(run=identifier)
        assert any(entry["kind"] == "tickets_admitted" for entry in log)
        # The factory keeps the specification and the tickets, not the project.
        documents = service.detail(identifier)["documents"]
        assert documents["specification"] == "SPEC: pay by card"
        folder = tmp_path / "artifacts" / "app" / identifier
        assert documents["folder"] == str(folder)
        assert "SPEC: pay by card" in (folder / "spec.md").read_text(encoding="utf-8")
        assert "## api · Pay endpoint" in (folder / "tickets.md").read_text(encoding="utf-8")
        assert not (root / "artifacts").exists()
        assert service.state()["totals"]["calls"] == 0
    finally:
        service.coordinator.close()


def test_failed_actions_are_recorded_in_the_flight_log(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        try:
            service.mutate("resume", {"id": "missing", "version": 0})
        except KeyError:
            pass
        entries = service.flight(level="error")
        assert entries[-1]["action"] == "resume" and entries[-1]["run"] == "missing"
        raw = (tmp_path / "ui.flight.jsonl").read_text(encoding="utf-8")
        assert all(json.loads(line)["kind"] for line in raw.splitlines())
    finally:
        service.coordinator.close()


def test_queue_revives_only_limit_blocks_after_a_rest(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "project"
        root.mkdir()
        argv = canonical({"argv": [sys.executable, "-c", "pass"]})
        flow = Workflow(
            "limits",
            "work",
            (
                Step("work", "check", "command", transitions=(("passed", "end"),), config=argv),
                Step("end", "finish"),
            ),
        )
        definition = service.engine.store.publish(flow)
        for name in ("limited", "broken"):
            service.mutate("create", {"id": name, "definition": definition, "workspace": str(root)})
        engine = service.engine
        engine.block("limited", time.time(), WAIT_RETRY_LIMIT)
        engine.block("broken", time.time(), "Provider protocol: bad JSON")
        queue = service.queue
        queue.settings["running"] = True
        queue._watch(time.time())
        assert engine.store.get("limited").status == "blocked", "too early: limits rest first"
        later = time.time() + REVIVE_AFTER + 60
        queue._watch(later)
        assert engine.store.get("limited").status == "ready"
        assert engine.store.get("broken").status == "blocked"
        assert any(e["kind"] == "revived" for e in service.flight(run="limited"))
        queue.settings["revive"] = False
        engine.block("limited", time.time(), WAIT_RETRY_LIMIT)
        queue._watch(later + REVIVE_AFTER * 2)
        assert engine.store.get("limited").status == "blocked"
    finally:
        service.coordinator.close()


def test_bulk_resume_respects_dependencies_and_project(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        roots = {}
        for name in ("app", "other"):
            roots[name] = tmp_path / name
            roots[name].mkdir()
            service.mutate("project", {"id": name, "name": name, "workspace": str(roots[name])})
        flow = Workflow("empty", "done", (Step("done", "finish"),))
        definition = service.engine.store.publish(flow)
        create = service.tasks.create
        create({"id": "base", "project": "app", "definition": definition})
        create(
            {"id": "child", "project": "app", "definition": definition, "dependencies": ["base"]}
        )
        create({"id": "elsewhere", "project": "other", "definition": definition})
        runs = {r["id"]: r for r in service.state()["runs"]}
        assert runs["child"]["pending_dependencies"] == ["base"]

        filtered = service.mutate(
            "resume-many", {"project": "app", "scope": "all", "kind": "ticket"}
        )
        assert filtered["changed"] == []
        result = service.mutate("resume-many", {"project": "app", "scope": "startable"})
        assert result == {"changed": ["base"], "skipped": 0}
        result = service.mutate("resume-many", {"project": "app", "scope": "all"})
        assert result["changed"] == ["child"]
        assert service.engine.store.get("elsewhere").paused
        paused = service.mutate("pause-many", {"project": "app"})
        assert sorted(paused["changed"]) == ["base", "child"]
        assert any(e["kind"] == "bulk_resume" for e in service.flight())
    finally:
        service.coordinator.close()


def test_bulk_resume_by_plan_ids_and_dependencies(tmp_path):
    """A plan starts as a unit: its filter is honoured and outside prerequisites can follow."""
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "app"
        root.mkdir()
        service.mutate("project", {"id": "app", "name": "app", "workspace": str(root)})
        flow = Workflow("empty", "done", (Step("done", "finish"),))
        definition = service.engine.store.publish(flow)

        def create(identifier, plan, dependencies=()):
            service.engine.create(
                identifier, definition, root, "", "rev", time.time(), tuple(dependencies)
            )
            service.catalog.save_task(
                identifier,
                TaskRecord(project="app", kind="ticket", title=identifier, plan=plan),
            )

        create("root", "p0")
        create("base", "p0", ["root"])
        create("first", "p1", ["base"])
        create("second", "p1", ["first"])
        create("other", "p2")
        state = {r["id"]: r for r in service.state()["runs"]}
        assert state["second"]["dependencies"] == ["first"]

        result = service.mutate("resume-many", {"project": "app", "scope": "all", "plan": "p1"})
        assert sorted(result["changed"]) == ["first", "second"]
        assert service.engine.store.get("base").paused
        service.mutate("pause-many", {"project": "app", "plan": "p1"})

        result = service.mutate(
            "resume-many",
            {"project": "app", "scope": "all", "plan": "p1", "with_dependencies": True},
        )
        assert sorted(result["changed"]) == ["base", "first", "root", "second"]
        assert service.engine.store.get("other").paused

        service.mutate("pause-many", {"project": "app"})
        result = service.mutate(
            "resume-many", {"project": "app", "scope": "all", "ids": ["second"]}
        )
        assert result["changed"] == ["second"]
        entry = [e for e in service.flight() if e["kind"] == "bulk_resume"][-1]
        assert entry["tasks"] == 1 and entry["with_dependencies"] is False
        # An already resumed task still pulls in what it waits for.
        result = service.mutate(
            "resume-many",
            {"project": "app", "scope": "all", "ids": ["second"], "with_dependencies": True},
        )
        assert sorted(result["changed"]) == ["base", "first", "root"]
    finally:
        service.coordinator.close()


def test_live_progress_and_question_events(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "project"
        root.mkdir()
        flow = Workflow(
            "ask",
            "ask",
            (
                Step("ask", "human", prompt="?", transitions=(("answered", "done"),)),
                Step("done", "finish"),
            ),
        )
        definition = service.engine.store.publish(flow)
        service.mutate("create", {"id": "q", "definition": definition, "workspace": str(root)})
        assert service.live("q")["active"] is False
        service.queue._watch(time.time())
        service.mutate("resume", {"id": "q", "version": 0})
        service.coordinator.tick()
        service.queue._watch(time.time())
        assert [e["kind"] for e in service.flight(run="q")].count("question") == 1
        progress = service.live("q")
        assert progress["active"] and progress["step"] == "ask" and progress["streams"] == {}
        assert service.state()["versions"]["engine"] == service.versions["engine"]
    finally:
        service.coordinator.close()
