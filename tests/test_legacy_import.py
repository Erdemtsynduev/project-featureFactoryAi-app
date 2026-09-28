"""Legacy portfolio translation: explicit paused tickets, never inferred acceptance."""

import json
import sqlite3
import subprocess
import sys

import pytest
from sdd_core.graph import validate
from sdd_factory.legacy.importer import apply, observe, read_source
from sdd_factory.legacy.translate import EMPTY_TREE, Environment, run_id, translate
from sdd_runtime.engine import Engine
from sdd_storage.store import Store
from sdd_ui.workspace import WorkspaceCatalog

PYTHON = sys.executable


def unit(identifier, source, status="ready", depends=(), checks=(), repos=("libraries/a",)):
    return {
        "status": status,
        "contract": {
            "id": identifier,
            "source_id": source,
            "source_ids": [source],
            "acceptance": f"Acceptance of {identifier}",
            "depends_on": list(depends),
            "repos": list(repos),
            "checks": list(checks),
            "manual_review": "Inspect the report",
            "context": ["docs/notes.md"],
        },
    }


def portfolio(**overrides):
    document = {
        "status": "blocked",
        "paused": False,
        "active": None,
        "allow_commits": True,
        "sources": {
            "1:A": {"plan": "1", "requirement": "**A** first", "path": "plans/1.md", "mark": "x"},
            "1:B": {"plan": "1", "requirement": "B second", "path": "plans/1.md"},
            "1:C": {"plan": "1", "requirement": "C third", "path": "plans/1.md"},
            "2:X": {"plan": "2", "requirement": "Not decomposed", "path": "plans/2.md"},
        },
        "units": {
            "1:A.1": unit("1:A.1", "1:A", status="accepted"),
            "1:B.1": unit(
                "1:B.1",
                "1:B",
                depends=["1:A.1"],
                checks=[
                    {
                        "argv": ["python", "tools/check.py", "--base", "{base:libraries/a}"],
                        "cwd": ".",
                        "timeout": 60,
                    },
                    {
                        "argv": ["git", "-C", "libraries/new", "diff", "{base:libraries/new}"],
                        "cwd": ".",
                        "timeout": 30,
                    },
                ],
            ),
            "1:C.1": unit("1:C.1", "1:C", depends=["1:B"]),
            "1:C.2": unit("1:C.2", "1:C", status="blocked", depends=["2:X"]),
            "1:C.3": unit("1:C.3", "1:C", depends=["1:C.2"]),
            "1:C.4": unit("1:C.4", "1:C", checks=[{"argv": ["node", "x.js"], "timeout": 5}]),
        },
        "specs": {"1": {"requirements": {"1:B": {"constraints": ["Keep the core pure"]}}}},
        "decisions": {"1:B": {"guidance": "Use the recorded approach"}},
    }
    document.update(overrides)
    return document


ENV = Environment(
    {"python": "C:/Python/python.exe", "git": "C:/Git/git.exe"}, {"libraries/a": "abc123"}
)


def test_translation_orders_dependencies_and_resolves_checks():
    plan = translate(portfolio(), ENV)
    ids = [ticket.legacy_id for ticket in plan.tickets]
    # Undecomposed plan rows become requirements first; tickets follow in dependency order.
    assert ids == ["2:X", "1:B.1", "1:C.2", "1:C.1", "1:C.3"]
    assert dict(plan.skipped) == {"1:C.4": "Check executable is not resolved: node"}
    assert "1:A.1" in plan.satisfied
    requirement_plan, first, waiting, second, _ = plan.tickets
    assert requirement_plan.kind == "requirement" and requirement_plan.scope == ("plans",)
    assert (
        requirement_plan.workflow.id == "feature" and "Not decomposed" in requirement_plan.context
    )
    assert waiting.dependencies == ("2_X",) and waiting.parent == "1_C"
    assert [(p.id, p.requirements, p.accepted, p.tickets) for p in plan.plans] == [
        ("1", 3, 1, 6),
        ("2", 1, 0, 0),
    ]
    assert first.id == "1_B_1" and first.scope == ("libraries/a",) and first.dependencies == ()
    assert second.dependencies == ("1_B_1",)
    validate(first.workflow)
    checks = [s for s in first.workflow.steps if s.kind == "check"]
    assert [s.id for s in checks] == ["check_1", "check_2"]
    configs = [json.loads(s.config) for s in checks]
    assert configs[0]["argv"] == ["C:/Python/python.exe", "tools/check.py", "--base", "abc123"]
    assert configs[1]["argv"][-1] == EMPTY_TREE
    assert checks[0].timeout == 60 and configs[0]["title"] == "check.py"
    assert "Commits are authorized" in first.workflow.step("implement").prompt
    assert "Keep the core pure" in first.context and "Use the recorded approach" in first.context
    assert "Acceptance of 1:B.1" in first.context and "Response language: Russian" in first.context
    assert first.title == "B second"


def test_translation_refuses_an_active_legacy_scheduler():
    with pytest.raises(ValueError, match="active"):
        translate(portfolio(active={"id": "x"}), ENV)
    with pytest.raises(ValueError, match="running"):
        translate(portfolio(status="running"), ENV)
    assert translate(portfolio(status="running", paused=True), ENV).tickets


def test_run_ids_are_valid_and_stable():
    assert run_id("104:ECL-02.1") == "104_ECL-02_1"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_apply_creates_paused_scoped_runs_idempotently(tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "libraries" / "a"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "T")
    (repo / "file.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    legacy = tmp_path / "legacy" / "portfolios" / "p1.sqlite3"
    legacy.parent.mkdir(parents=True)
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE portfolio(id INTEGER PRIMARY KEY, revision INTEGER, data TEXT)")
        db.execute("INSERT INTO portfolio VALUES(1, 1, ?)", (json.dumps(portfolio()),))
    source = read_source(legacy, workspace)
    env = observe(source, PYTHON)
    assert env.heads["libraries/a"] and "libraries/new" not in env.heads
    plan = translate(source.document, env)
    store = Store(tmp_path / "control" / "ui.db")
    engine, catalog = Engine(store), WorkspaceCatalog(store.catalog(), store.path, store.workflow)
    first = apply(engine, catalog, source, plan, "ru")
    assert first["created"] == ["2_X", "1_B_1", "1_C_2", "1_C_1", "1_C_3"]
    run = engine.store.get("1_B_1")
    assert run.paused and run.status == "ready" and run.step == "implement"
    with store.unit() as db:
        assert db.location("1_B_1")[1] == str(repo.resolve())
    assert catalog.task_metadata()["1_C_1"]["legacy_id"] == "1:C.1"
    assert apply(engine, catalog, source, plan, "ru")["kept"] == first["created"]
    assert catalog.task_metadata()["2_X"]["kind"] == "feature"
    assert {p["id"] for p in catalog.plans()[first["project"]]} == {"1", "2"}
    with sqlite3.connect(legacy) as db:
        assert json.loads(db.execute("SELECT data FROM portfolio").fetchone()[0]) == portfolio()
