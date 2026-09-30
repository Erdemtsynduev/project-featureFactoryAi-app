"""Plans are sources of features: one plan, one specification, its tickets."""

import pytest
from sdd_core.models import Step, Workflow
from sdd_factory.admission import ticket_scope
from sdd_factory.model import TaskRecord
from sdd_factory.plans import feature_brief
from sdd_factory.sources.markdown import parse_plan
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


def test_rebuild_plans_never_started_work_again_and_keeps_started_work(tmp_path, monkeypatch):
    service, root, flow = game(tmp_path, monkeypatch)
    try:
        engine, catalog = service.engine, service.catalog
        assert service.mutate("plans-sync", {"project": "game"})["created"] == ["feature_110"]
        engine.create("started", flow, root, "", "rev", 1)
        catalog.save_task(
            "started", TaskRecord.load({"project": "game", "kind": "feature", "plan": "110"})
        )
        engine.command("started", "resume", "go", 0, 2)
        engine.dispatch("started", 3, "a1")  # started work survives

        rebuilt = service.mutate("plans-rebuild", {"project": "game"})
        assert rebuilt["removed"] == 1 and rebuilt["created"] == ["feature_110"]
        assert (tmp_path / rebuilt["backup"]).is_file()
        runs = {r["id"] for r in service.state()["runs"]}
        assert {"started", "feature_110"} <= runs
        assert catalog.task_metadata()["feature_110"]["rows"] == ["FH-02", "FH-03"]
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


def test_a_feature_brief_names_delivered_tickets_so_they_are_not_redone():
    plan = parse_plan("plans/110_RALLY_PLAN.md", PLAN)
    brief = feature_brief(
        plan,
        list(plan.rows[1:]),
        [],
        ["110-T50 — Брод"],
        ["110-T28 — Удары (branch ffai/110-T28)"],
    )
    assert "do not redo them" in brief and "110-T50 — Брод" in brief
    assert "plan their scope again" in brief and "ffai/110-T28" in brief
