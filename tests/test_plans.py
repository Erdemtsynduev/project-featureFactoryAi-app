"""Plans are sources of features: one plan, one specification, its tickets."""

import pytest
from sdd_core.models import Step, Workflow
from sdd_factory.model import TaskRecord
from sdd_factory.sources.markdown import parse_plan
from sdd_factory.tasks import ticket_scope
from sdd_ui.service import WorkspaceService

PLAN = """# Rally: большое обновление

> Статус: 1 `[x]`, 1 `[~]`, 1 `[ ]` из 3.

## Правило: сначала ресерч

У каждой новой возможности до строки реализации есть строка ресерча.

## Строки

- [x] **FH-01** — Баг «след перед машиной».
      Закрыто: исправлено.
- [~] **FH-02** — Антитайлинг поверхностей.
      Приёмка: метрика повторяемости ниже порога.
- [ ] **FH-03** — Ресерч кабины: как делают эталоны.
"""

STEPPED = Workflow(
    "x",
    "work",
    (Step("work", "operation", "fake", transitions=(("done", "done"),)), Step("done", "finish")),
)


def test_parse_plan_reads_rows_marks_details_and_rules():
    plan = parse_plan("plans/110_RALLY_PLAN.md", PLAN)
    assert plan.key == "110" and plan.title == "110 · Rally: большое обновление"
    assert [(r.id, r.mark, r.open) for r in plan.rows] == [
        ("FH-01", "x", False),
        ("FH-02", "~", True),
        ("FH-03", " ", True),
    ]
    assert plan.rows[1].detail == ("Приёмка: метрика повторяемости ниже порога.",)
    assert "сначала ресерч" in plan.body
    with pytest.raises(ValueError, match="Duplicate row"):
        parse_plan("110_X.md", PLAN + "- [ ] **FH-03** — again\n")
    with pytest.raises(ValueError, match="three-digit"):
        parse_plan("rally.md", PLAN)


def game(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    root = tmp_path / "game"
    (root / "plans").mkdir(parents=True)
    (root / "plans" / "110_RALLY_PLAN.md").write_text(PLAN, encoding="utf-8")
    service.mutate(
        "project", {"id": "game", "name": "Game", "workspace": str(root), "plans_folder": "plans"}
    )
    flow = service.engine.store.publish(STEPPED)
    monkeypatch.setattr(
        service.flows, "ensure", lambda name, project, language, repositories=(): flow
    )
    return service, root, flow


def test_sync_makes_one_feature_per_plan_and_a_follow_up_for_new_rows(tmp_path, monkeypatch):
    service, root, _ = game(tmp_path, monkeypatch)
    try:
        assert service.mutate("plans-sync", {"project": "game"}) == {
            "plans": 1,
            "created": ["feature_110"],
        }
        meta = service.catalog.task_metadata()["feature_110"]
        assert meta["kind"] == "feature" and meta["rows"] == ["FH-02", "FH-03"]
        assert service.engine.store.get("feature_110").paused
        with service.engine.store.unit() as unit:
            brief = unit.context("feature_110")
        assert "plans/110_RALLY_PLAN.md" in brief and "FH-02 (partial" in brief
        assert "FH-01" not in brief.split("Scope:")[1]
        assert service.mutate("plans-sync", {"project": "game"})["created"] == []

        # A research row adds a row: it becomes a follow-up feature on its own.
        path = root / "plans" / "110_RALLY_PLAN.md"
        path.write_text(PLAN + "- [ ] **FH-04** — Кабина: руль и приборы.\n", encoding="utf-8")
        follow = service.mutate("plans-sync", {"project": "game", "plan": "110"})
        assert follow["created"] == ["feature_110_2"]
        assert service.catalog.task_metadata()["feature_110_2"]["rows"] == ["FH-04"]
        plan = service.state()["plans"]["game"][0]
        assert plan["open"] == 2 and plan["partial"] == 1 and plan["requirements"] == 4
        with pytest.raises(ValueError, match="No plan 111"):
            service.mutate("plans-sync", {"project": "game", "plan": "111"})
    finally:
        service.coordinator.close()


def test_rebuild_replaces_per_row_requirements_and_carries_their_drafts(tmp_path, monkeypatch):
    service, root, flow = game(tmp_path, monkeypatch)
    try:
        engine, catalog = service.engine, service.catalog

        def legacy(identifier, kind, context="", **extra):
            engine.create(identifier, flow, root, context, "rev", 1)
            catalog.save_task(
                identifier,
                TaskRecord.load({"project": "game", "kind": kind, "plan": "110", **extra}),
            )

        legacy("110_FH-02", "requirement", "Recorded acceptance draft:\nСтыки не видны с 5 м.")
        legacy("110_FH-03", "requirement")
        legacy("110_FH-09", "requirement")
        legacy("110_FH-03_1", "ticket", legacy_id="110:FH-03.1", parent="110_FH-03")
        engine.command("110_FH-09", "resume", "go", 0, 2)
        engine.dispatch("110_FH-09", 3, "started")  # started work survives

        rebuilt = service.mutate("plans-rebuild", {"project": "game"})
        assert rebuilt["removed"] == 2 and rebuilt["created"] == ["feature_110"]
        assert (tmp_path / rebuilt["backup"]).is_file()
        runs = {r["id"] for r in service.state()["runs"]}
        assert {"110_FH-09", "110_FH-03_1", "feature_110"} <= runs
        assert not {"110_FH-02", "110_FH-03"} & runs
        # FH-03 is already decomposed into a queued legacy ticket: only FH-02 is new scope.
        assert catalog.task_metadata()["feature_110"]["rows"] == ["FH-02"]
        with engine.store.unit() as unit:
            brief = unit.context("feature_110")
        assert "Стыки не видны с 5 м." in brief and "110_FH-03_1" in brief
    finally:
        service.coordinator.close()


def test_ticket_scope_is_the_owning_repositories(tmp_path):
    for repo in ("framework-rally", "libraries/terrain"):
        (tmp_path / repo / ".git").mkdir(parents=True)
    assert ticket_scope(tmp_path, ("libraries/terrain/scripts/a.gd", "framework-rally")) == (
        "framework-rally",
        "libraries/terrain",
    )
    assert ticket_scope(tmp_path, ("docs/notes.md",)) == ()  # not a repository: whole workspace
    assert ticket_scope(tmp_path, ("../outside",)) == ()
    assert ticket_scope(tmp_path, ()) == ()


def test_store_refuses_to_discard_started_or_needed_runs(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "work"
        root.mkdir()
        engine = service.engine
        flow = engine.store.publish(STEPPED)
        engine.create("base", flow, root, "", "rev", 1)
        engine.create("child", flow, root, "", "rev", 1, ("base",))
        with pytest.raises(ValueError, match="child depends on base"):
            engine.store.discard(("base",))
        assert engine.store.discard(("base", "child")) == ("base", "child")
        engine.create("run", flow, root, "", "rev", 1)
        engine.command("run", "resume", "r", 0, 2)
        engine.dispatch("run", 3, "a")
        with pytest.raises(ValueError, match="has started"):
            engine.store.discard(("run",))
    finally:
        service.coordinator.close()


def test_task_blocked_by_the_old_dispatch_race_is_released_on_start(tmp_path):
    database = tmp_path / "ui.db"
    service = WorkspaceService(database)
    root = tmp_path / "work"
    root.mkdir()
    flow = service.engine.store.publish(Workflow("empty", "done", (Step("done", "finish"),)))
    service.engine.create("stuck", flow, root, "", "rev", 1)
    service.engine.block("stuck", 2, "Run is not dispatchable")
    service.coordinator.close()
    restarted = WorkspaceService(database)
    try:
        run = restarted.engine.store.get("stuck")
        assert run.status == "ready" and run.paused
        assert any(e["kind"] == "unblocked" for e in restarted.flight(run="stuck"))
    finally:
        restarted.coordinator.close()


def test_a_project_without_a_plans_folder_has_no_plans(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "app"
        root.mkdir()
        service.mutate("project", {"id": "app", "name": "App", "workspace": str(root)})
        assert service.catalog.project("app")["plans_folder"] == ""
        with pytest.raises(ValueError, match="no plans folder"):
            service.mutate("plans-sync", {"project": "app"})
        with pytest.raises(ValueError, match="not a folder"):
            service.mutate(
                "project",
                {"id": "app", "name": "App", "workspace": str(root), "plans_folder": "missing"},
            )
    finally:
        service.coordinator.close()
