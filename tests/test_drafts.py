"""A source item is imported once as a draft; the lead cuts it into features."""

import sys
from pathlib import Path

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Result, Step, Workflow
from sdd_core.tickets import TicketDraft, covers_rows, draft_of, labels_of
from sdd_core.tracking import WorkItem
from sdd_factory.admission import ticket_scope
from sdd_factory.briefs import SourceWork, draft_brief, feature_brief
from sdd_factory.model import PLANNING_SCOPE, TaskRecord
from sdd_factory.sources.markdown import parse_plan
from sdd_factory.trackers import ProjectSources
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
SOURCE = "plans/110_RALLY_PLAN.md"
ARGV = [sys.executable, "-c", "pass"]


def check(step: str, produces: str, transitions: tuple[tuple[str, str], ...]) -> Step:
    # A check stands in for an agent: the test supplies its result.
    config = canonical({"argv": ARGV, "produces": produces})
    return Step(step, "check", "command", transitions=transitions, config=config)


def approval(rework: str) -> Step:
    routes = (("approved", "accepted"), ("rework", rework))
    return Step("approve", "human", prompt="Approve", transitions=routes)


FLOWS = {
    "draft": Workflow(
        "draft",
        "groom",
        (
            check("groom", "features", (("done", "approve"),)),
            approval("groom"),
            Step("accepted", "finish"),
        ),
    ),
    "feature": Workflow(
        "feature",
        "spec",
        (
            check("spec", "specification", (("done", "tickets"),)),
            check("tickets", "tickets", (("done", "approve"),)),
            approval("spec"),
            Step("accepted", "finish"),
        ),
    ),
    "ticket": Workflow(
        "ticket", "work", (check("work", "", (("done", "end"),)), Step("end", "finish"))
    ),
}


@pytest.fixture
def game(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    root = tmp_path / "game"
    (root / "plans").mkdir(parents=True)
    (root / SOURCE).write_text(PLAN, encoding="utf-8")
    service.mutate(
        "project", {"id": "game", "name": "Game", "workspace": str(root), "plans_folder": "plans"}
    )
    digests = {name: service.engine.store.publish(flow) for name, flow in FLOWS.items()}
    monkeypatch.setattr(
        service.flows, "ensure", lambda name, project, language, repositories=(): digests[name]
    )
    yield service, root
    service.coordinator.close()


def finish(service: WorkspaceService, run_id: str, attempt: str, reason: str = "ok", **data):
    engine = service.engine
    run = engine.dispatch(run_id, 10, attempt)
    result = Result(attempt, run.generation, "done", reason, run.revision, data=canonical(data))
    return engine.complete(run_id, result, 11)


def answer(service: WorkspaceService, run_id: str, outcome: str, attempt: str, reply: str = ""):
    waiting = service.engine.dispatch(run_id, 12, attempt)
    request = {"id": run_id, "outcome": outcome, "answer": reply, "version": waiting.version}
    return service.mutate("answer", request)


def start(service: WorkspaceService, run_id: str) -> None:
    version = service.engine.store.get(run_id).version
    service.mutate("resume", {"id": run_id, "version": version})


# Sources and rules ---------------------------------------------------------------


def test_parse_plan_reads_rows_marks_details_and_rules():
    plan = parse_plan(SOURCE, PLAN)
    assert plan.key == "110" and plan.title == "110 · Rally: большое обновление"
    assert [(r.id, r.mark, r.open) for r in plan.rows] == [
        ("FH-01", "x", False),
        ("FH-02", "~", True),
        ("FH-03", " ", True),
    ]
    assert plan.rows[1].detail == ("Приёмка: метрика повторяемости ниже порога.",)
    assert "сначала ресерч" in plan.body and plan.source == SOURCE
    with pytest.raises(ValueError, match="Duplicate row"):
        parse_plan("110_X.md", PLAN + "- [ ] **FH-03** — again\n")
    with pytest.raises(ValueError, match="three-digit"):
        parse_plan("rally.md", PLAN)


def test_a_cut_covers_every_row_exactly_once():
    cut = (TicketDraft("F1", "A", covers=("R1", "R2")), TicketDraft("F2", "B", covers=("R3",)))
    covers_rows(cut, ("R1", "R2", "R3"))
    covers_rows((TicketDraft("F1", "Idea"),), ())
    with pytest.raises(ValueError, match="R4 is covered by no feature"):
        covers_rows(cut, ("R1", "R2", "R3", "R4"))
    with pytest.raises(ValueError, match="R3 is not a row of this draft"):
        covers_rows(cut, ("R1", "R2"))
    twice = (*cut, TicketDraft("F3", "C", covers=("R1",)))
    with pytest.raises(ValueError, match="R1 is covered by F1, F3"):
        covers_rows(twice, ("R1", "R2", "R3"))


def test_labels_are_normalised_short_tags():
    assert labels_of(["Shader Lib", "shader-lib", " Звук "]) == ("shader-lib", "звук")
    assert draft_of({"id": "F1", "title": "A", "labels": ["UI"]}).labels == ("ui",)
    with pytest.raises(ValueError, match="without punctuation: a/b"):
        labels_of(["a/b"])
    with pytest.raises(ValueError, match="At most 3 labels"):
        labels_of(["a", "b", "c", "d"])


def test_briefs_carry_the_scope_and_the_work_already_cut_from_the_source():
    plan = parse_plan(SOURCE, PLAN)
    work = SourceWork(
        queued=("110-T10 — Петли (blocked)",),
        delivered=("110-T50 — Брод",),
        superseded=("110-T28 — Удары (branch ffai/110-T28)",),
    )
    brief = draft_brief(plan, work, {"sound", "shaders"})
    assert "plans/110_RALLY_PLAN.md" in brief and "сначала ресерч" in brief
    assert "do not redo them" in brief and "110-T50 — Брод" in brief
    assert "plan their scope again" in brief and "ffai/110-T28" in brief
    assert "Labels this project already uses: shaders, sound" in brief
    idea = draft_brief(WorkItem("idea", "Idea", "Make the gearbox audible", ()))
    assert "no rows" in idea and "Make the gearbox audible" in idea and "Labels" not in idea
    cut = TicketDraft("F1", "Tiling", "Less repetition", covers=("FH-02",), labels=("shaders",))
    feature = feature_brief(plan, cut, work)
    assert feature.startswith("Feature F1: Tiling\nGoal: Less repetition\nLabels: shaders")
    assert "FH-02 (partial" in feature and "FH-03" not in feature and "110-T10" in feature


# Import ------------------------------------------------------------------------


def test_import_makes_one_draft_per_item_and_a_follow_up_for_new_rows(game):
    service, root = game
    assert service.mutate("drafts-import", {"project": "game"}) == {
        "items": 1,
        "created": ["draft_110"],
    }
    meta = service.catalog.task_metadata()["draft_110"]
    assert meta["kind"] == "draft" and meta["rows"] == ["FH-02", "FH-03"]
    assert meta["source"] == SOURCE and "plan" not in meta
    assert service.engine.store.get("draft_110").paused, "importing starts nothing"
    with service.engine.store.unit() as unit:
        brief = unit.context("draft_110")
        assert Path(unit.location("draft_110").claim).name == PLANNING_SCOPE
    assert "FH-02 (partial" in brief and "сначала ресерч" in brief and "FH-01" not in brief
    assert service.mutate("drafts-import", {"project": "game"})["created"] == []

    # A research row adds a row: it becomes a follow-up draft on its own.
    (root / SOURCE).write_text(PLAN + "- [ ] **FH-04** — Кабина: руль.\n", encoding="utf-8")
    follow = service.mutate("drafts-import", {"project": "game", "item": "110"})
    assert follow["created"] == ["draft_110_2"]
    assert service.catalog.task_metadata()["draft_110_2"]["rows"] == ["FH-04"]
    assert "plans" not in service.state(), "nothing follows a plan file after the import"
    with pytest.raises(ValueError, match="No item 111"):
        service.mutate("drafts-import", {"project": "game", "item": "111"})


def test_a_project_takes_drafts_from_every_source_it_names(tmp_path):
    class Issues:
        id = "fake"

        def __init__(self, settings):
            pass

        def items(self, workspace):
            return [WorkItem("ENG-7", "ENG-7 · Gearbox", "Make it audible", (), link="fake:7")]

        def publish(self, update):
            raise AssertionError("an import publishes nothing")

    sources = ProjectSources(lambda: {"fake": Issues})
    root = tmp_path / "app"
    (root / "plans").mkdir(parents=True)
    (root / SOURCE).write_text(PLAN, encoding="utf-8")
    project = {"workspace": str(root), "plans_folder": "plans", "tracker": {"kind": "fake"}}
    found = [item for source in sources.sources(project) for item in source.items(str(root))]
    assert [(item.key, item.source) for item in found] == [("110", SOURCE), ("ENG-7", "fake:7")]
    with pytest.raises(ValueError, match="no source of drafts"):
        sources.sources({"workspace": str(root)})


def test_an_item_without_rows_is_taken_once(game, monkeypatch):
    service, _ = game
    item = WorkItem("ENG-7", "ENG-7 · Gearbox", "Make it audible", (), link="fake:7")
    monkeypatch.setattr(service.drafts, "sources", lambda project: [type("S", (), {})()])
    monkeypatch.setattr(service.drafts, "_items", lambda project, workspace, only: [item])
    assert service.mutate("drafts-import", {"project": "game"})["created"] == ["draft_ENG-7"]
    record = service.catalog.task("draft_ENG-7")
    assert (record.source, record.link, record.rows) == ("fake:7", "fake:7", ())
    assert service.mutate("drafts-import", {"project": "game"})["created"] == []


# The cut -----------------------------------------------------------------------


def test_the_lead_cuts_a_draft_into_features_a_person_approves(game, tmp_path):
    service, _ = game
    engine, catalog = service.engine, service.catalog
    service.mutate("drafts-import", {"project": "game"})
    start(service, "draft_110")
    lone = [{"id": "F1", "title": "Tiling", "covers": ["FH-02"]}]
    finish(service, "draft_110", "g1", features=lone)
    assert [f["id"] for f in service.detail("draft_110")["tickets"]] == ["F1"]
    waiting = engine.dispatch("draft_110", 12, "h1")
    approve = {"id": "draft_110", "outcome": "approved", "answer": "", "version": waiting.version}
    with pytest.raises(ValueError, match="FH-03 is covered by no feature"):
        service.mutate("answer", approve)
    assert engine.store.get("draft_110").active is not None, "the approval still waits"
    service.mutate("answer", {**approve, "outcome": "rework", "answer": "cover FH-03"})

    cut = [
        {
            "id": "F1",
            "title": "Tiling",
            "goal": "Less repetition",
            "covers": ["FH-02"],
            "labels": ["Shaders", "rally"],
        },
        {"id": "F2", "title": "Cabin research", "covers": ["FH-03"], "depends_on": ["F1"]},
    ]
    finish(service, "draft_110", "g2", features=cut)
    admitted = answer(service, "draft_110", "approved", "h2")["admitted"]
    first, second = "draft_110-F1", "draft_110-F2"
    assert admitted == [first, second]
    assert catalog.task_metadata()[first] == {
        "kind": "feature",
        "labels": ["shaders", "rally"],
        "language": "ru",
        "parent": "draft_110",
        "project": "game",
        "rows": ["FH-02"],
        "source": SOURCE,
        "title": "Tiling",
    }
    with engine.store.unit() as unit:
        assert set(unit.dependency_edges()) == {(second, first)}
        brief = unit.context(first)
        assert Path(unit.location(first).claim).name == PLANNING_SCOPE
    assert "Feature F1: Tiling" in brief and "FH-02 (partial" in brief
    assert "сначала ресерч" in brief and "FH-03" not in brief, "only its rows, with the rules"
    assert all(engine.store.get(key).paused for key in admitted), "a person starts each feature"
    folder = tmp_path / "artifacts" / "game" / "draft_110"
    assert "## F2 · Cabin research" in (folder / "features.md").read_text(encoding="utf-8")
    runs = {r["id"]: r for r in service.state()["runs"]}
    assert runs["draft_110"]["progress"] == {"done": 0, "total": 2}
    assert runs[second]["ticket"]["wave"] == 2 and runs[second]["ticket"]["after"] == ["F1"]
    assert any(e["kind"] == "features_admitted" for e in service.flight(run="draft_110"))

    # A feature is planned as before; the features waiting for it learn its tickets.
    start(service, first)
    finish(service, first, "s1", reason="SPEC: tiling metric")
    finish(service, first, "t1", tickets=[{"id": "T1", "title": "Hex tiling"}])
    (ticket,) = answer(service, first, "approved", "h3")["admitted"]
    record = catalog.task(ticket)
    assert (record.kind, record.parent, record.source) == ("ticket", first, SOURCE)
    assert record.labels == ("shaders", "rally"), "a ticket keeps its feature's labels"
    with engine.store.unit() as unit:
        told = unit.context(second)
    assert f"- {ticket} — Hex tiling" in told and "`after`" in told
    assert ticket in service.admission.known_tickets(second)
    assert engine.store.get(second).generation == 0, "being told starts nothing"


def test_rebuild_takes_in_again_what_never_started_and_keeps_started_work(game, tmp_path):
    service, root = game
    engine, catalog = service.engine, service.catalog
    service.mutate("drafts-import", {"project": "game"})
    start(service, "draft_110")
    cut = [
        {"id": "F1", "title": "Tiling", "covers": ["FH-02"]},
        {"id": "F2", "title": "Cabin research", "covers": ["FH-03"]},
    ]
    finish(service, "draft_110", "g1", features=cut)
    answer(service, "draft_110", "approved", "h1")
    first, second = "draft_110-F1", "draft_110-F2"
    start(service, first)
    engine.dispatch(first, 13, "s1")  # started work survives

    rebuilt = service.mutate("drafts-rebuild", {"project": "game"})
    assert rebuilt["removed"] == 1 and rebuilt["reopened"] == ["draft_110"]
    assert (tmp_path / rebuilt["backup"]).is_file()
    assert rebuilt["created"] == ["draft_110_2"], "the rows of the removed feature only"
    runs = {r["id"] for r in service.state()["runs"]}
    assert {"draft_110", first, "draft_110_2"} <= runs and second not in runs
    closed, kept = catalog.task("draft_110"), catalog.task(first)
    assert closed.closed and closed.rows == (), "a closed draft covers no rows"
    assert (kept.parent, kept.origin, kept.rows) == ("", "draft_110", ("FH-02",))
    assert catalog.task("draft_110_2").rows == ("FH-03",)
    assert service.mutate("drafts-import", {"project": "game"})["created"] == []
    again = service.mutate("drafts-rebuild", {"project": "game", "item": "110"})
    assert again["removed"] == 1 and again["created"] == ["draft_110_2"], "rebuilt in place"


def test_bulk_start_takes_everything_cut_from_a_task_or_a_label(game):
    service, _ = game
    service.mutate("drafts-import", {"project": "game"})
    start(service, "draft_110")
    cut = [
        {"id": "F1", "title": "Tiling", "covers": ["FH-02"], "labels": ["shaders"]},
        {"id": "F2", "title": "Cabin", "covers": ["FH-03"], "labels": ["cabin"]},
    ]
    finish(service, "draft_110", "g1", features=cut)
    answer(service, "draft_110", "approved", "h1")
    every = {"project": "game", "scope": "all"}
    labelled = service.mutate("resume-many", {**every, "label": "cabin"})
    assert labelled["changed"] == ["draft_110-F2"]
    under = service.mutate("resume-many", {**every, "under": "draft_110"})
    assert under["changed"] == ["draft_110-F1"], "the rest of what was cut from the draft"
    assert service.mutate("pause-many", {"project": "game", "under": "draft_110-F1"}) == {
        "changed": [],
        "skipped": 0,
        "held": [],
    }


def test_a_project_without_sources_has_no_drafts(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "app"
        root.mkdir()
        service.mutate("project", {"id": "app", "name": "App", "workspace": str(root)})
        assert service.catalog.project("app")["plans_folder"] == ""
        with pytest.raises(ValueError, match="no source of drafts"):
            service.mutate("drafts-import", {"project": "app"})
        with pytest.raises(ValueError, match="not a folder"):
            service.mutate(
                "project",
                {"id": "app", "name": "App", "workspace": str(root), "plans_folder": "missing"},
            )
    finally:
        service.coordinator.close()


# Scopes and the store -------------------------------------------------------------

STEPPED = Workflow(
    "x",
    "work",
    (Step("work", "operation", "fake", transitions=(("done", "done"),)), Step("done", "finish")),
)


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


def test_a_legacy_plan_feature_is_taken_in_again_as_a_draft(game):
    """A board made before drafts: one feature per plan, its tickets never started."""
    service, root = game
    engine, catalog = service.engine, service.catalog
    flow = engine.store.publish(STEPPED)
    done = engine.store.publish(Workflow("done", "end", (Step("end", "finish"),)))
    engine.create("feature_110", done, root, "", "rev", 1)
    engine.command("feature_110", "resume", "go", 0, 2)
    assert engine.dispatch("feature_110", 3, "a").status == "accepted"
    engine.create("feature_110-T1", flow, root, "", "rev", 1)
    legacy = {"project": "game", "source": SOURCE}
    catalog.save_task("feature_110", TaskRecord(kind="feature", rows=("FH-02", "FH-03"), **legacy))
    catalog.save_task("feature_110-T1", TaskRecord(kind="ticket", parent="feature_110", **legacy))
    # A started ticket that was handed over: its scope is planned again, its branch reused.
    engine.create("feature_110-T2", flow, root, "", "rev", 1)
    handed = TaskRecord(kind="ticket", title="Кабина", superseded="feature_110", **legacy)
    catalog.save_task("feature_110-T2", handed)
    engine.command("feature_110-T2", "resume", "go-t2", 0, 2)
    engine.dispatch("feature_110-T2", 3, "w1")
    assert service.mutate("drafts-import", {"project": "game"})["created"] == []

    rebuilt = service.mutate("drafts-rebuild", {"project": "game"})
    assert rebuilt["removed"] == 1 and rebuilt["reopened"] == ["feature_110"]
    assert rebuilt["created"] == ["draft_110"]
    assert catalog.task("feature_110").closed and catalog.task("feature_110").rows == ()
    assert catalog.task("draft_110").rows == ("FH-02", "FH-03")
    with engine.store.unit() as unit:
        brief = unit.context("draft_110")
    assert "Superseded tickets from this source" in brief
    assert "- feature_110-T2 — Кабина (branch ffai/feature_110-T2)" in brief
    assert "feature_110-T2" not in service.admission.known_tickets("draft_110")
