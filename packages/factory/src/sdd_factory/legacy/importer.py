"""Move a paused sdd-orchestrator queue onto this engine as paused, explicit runs.

Run as `python -m sdd_factory.legacy <portfolio> --database <ui.db> [--apply]`.

The legacy database is opened read-only and is never modified. Created runs stay
paused; nothing starts until the operator resumes tasks and starts the queue.
Re-running the import is idempotent: existing identical runs are left alone.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sdd_core.codec import mapping, object_json, sequence, text
from sdd_core.models import Json
from sdd_runtime.composition import local_engine
from sdd_runtime.engine import Engine
from sdd_runtime.platform import NO_WINDOW

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.legacy.translate import (
    PLACEHOLDER,
    Environment,
    ImportPlan,
    plans_folder,
    translate,
)
from sdd_factory.model import TaskRecord


@dataclass(frozen=True)
class Source:
    path: Path
    workspace: Path
    project: str
    name: str
    document: dict[str, Json]


def read_source(path: Path, workspace: Path | None = None) -> Source:
    path = path.resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT data FROM portfolio WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Legacy portfolio has no state")
    document = object_json(str(row[0]))
    name = path.stem
    if workspace is None:
        registry = path.parent.parent / "registry.sqlite3"
        if not registry.is_file():
            raise ValueError("Workspace unknown: pass --workspace or keep the legacy registry")
        with sqlite3.connect(registry.as_uri() + "?mode=ro", uri=True) as db:
            found = db.execute(
                "SELECT workspace,name FROM projects WHERE id=?", (path.stem,)
            ).fetchone()
        if found is None:
            raise ValueError("Legacy registry has no project for this portfolio")
        workspace, name = Path(str(found[0])), str(found[1])
    return Source(path, workspace.resolve(strict=True), path.stem, name, document)


def _git_head(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    return result.stdout.decode().strip() if result.returncode == 0 else None


def project_python(explicit: str | None = None) -> str:
    """The project's interpreter, never this engine's own virtual environment."""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Python must be an existing absolute executable")
        return str(path)
    engine = Path(sys.prefix).resolve()
    for folder in filter(None, os.environ.get("PATH", "").split(os.pathsep)):
        candidate = shutil.which("python", path=folder)
        # Skip this engine's environment and the Microsoft Store launcher stub.
        if candidate and "WindowsApps" not in candidate:
            resolved = Path(candidate).resolve()
            if not resolved.is_relative_to(engine):
                return str(resolved)
    raise ValueError("No project Python on PATH; pass --python explicitly")


def observe(
    source: Source,
    python: str | None = None,
    language: str = "ru",
    isolated: bool = True,
    auto_resolve: bool = True,
) -> Environment:
    repos: set[str] = set()
    for raw in mapping(source.document.get("units", {})).values():
        contract = mapping(mapping(raw).get("contract", {}))
        repos.update(text(x, "repo") for x in sequence(contract.get("repos", [])))
        for check in sequence(contract.get("checks", [])):
            for argument in sequence(mapping(check).get("argv", [])):
                repos.update(
                    value for kind, value in PLACEHOLDER.findall(str(argument)) if kind == "base"
                )
    heads = {
        repo: head
        for repo in sorted(repos)
        if (head := _git_head(source.workspace / repo)) is not None
    }
    git = shutil.which("git")
    if git is None:
        raise ValueError("Git is required for the imported checks")
    return Environment(
        {"python": project_python(python), "git": str(Path(git).resolve())},
        heads,
        language,
        isolated=isolated,
        auto_resolve=auto_resolve,
    )


def summary(source: Source, plan: ImportPlan, env: Environment) -> dict[str, Json]:
    owner = source.workspace / ".plan-driver" / "engine-owner.json"
    return {
        "source": str(source.path),
        "workspace": str(source.workspace),
        "project": source.project,
        "legacy_status": str(source.document.get("status", "")),
        "legacy_owner": owner.is_file(),
        "executables": dict[str, Json](env.executables),
        "tickets": [
            {
                "id": t.id,
                "legacy_id": t.legacy_id,
                "title": t.title,
                "plan": t.plan,
                "kind": t.kind,
                "scope": list[Json](t.scope),
                "dependencies": list[Json](t.dependencies),
                "checks": sum(step.kind == "check" for step in t.workflow.steps),
                "notes": list[Json](t.notes),
            }
            for t in plan.tickets
        ],
        "skipped": [{"id": key, "reason": reason} for key, reason in plan.skipped],
        "satisfied": list[Json](plan.satisfied),
    }


def apply(
    engine: Engine, catalog: ProjectCatalog, source: Source, plan: ImportPlan, language: str
) -> dict[str, Json]:
    """Create every planned run paused; identical existing runs are kept."""
    project_id = re.sub(r"[^a-z0-9_-]", "-", source.name.lower()).strip("-")[:64] or source.project
    folder = plans_folder([info.path for info in plan.plans])
    if not catalog.project(project_id):
        catalog.save_project(
            {
                "id": project_id,
                "name": source.name,
                "workspace": str(source.workspace),
                "language": language,
                "checks": [],
                "plans_folder": folder if (source.workspace / folder).is_dir() else "",
                "isolation": any(
                    step.handler == "lane-integrate"
                    for ticket in plan.tickets
                    for step in ticket.workflow.steps
                ),
            }
        )
    catalog.save_plans(project_id, [asdict(info) for info in plan.plans])
    with engine.store.unit() as unit:
        existing = {run.id for run in unit.runs()}
    created: list[Json] = []
    kept: list[Json] = []
    for ticket in plan.tickets:
        definition = engine.store.publish(ticket.workflow)
        if ticket.id in existing:
            with engine.store.unit() as unit:
                same = (
                    unit.run(ticket.id).workflow_digest == definition
                    and unit.context(ticket.id) == ticket.context
                )
            if not same:
                raise ValueError(f"Run {ticket.id} exists with different inputs")
            kept.append(ticket.id)
            continue
        engine.create(
            ticket.id,
            definition,
            source.workspace,
            ticket.context,
            None,
            time.time(),
            ticket.dependencies,
            ticket.scope,
        )
        catalog.save_task(
            ticket.id,
            TaskRecord.load(
                {
                    "project": project_id,
                    "language": language,
                    "title": ticket.title,
                    "legacy_id": ticket.legacy_id,
                    "plan": ticket.plan,
                    "kind": ticket.kind,
                    "parent": ticket.parent,
                }
            ),
        )
        created.append(ticket.id)
    return {"project": project_id, "created": created, "kept": kept}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Legacy portfolios/<id>.sqlite3")
    parser.add_argument("--database", type=Path, required=True, help="Feature Factory database")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--python", help="Absolute project Python for imported checks")
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    parser.add_argument("--apply", action="store_true", help="Create paused runs")
    parser.add_argument(
        "--shared-workspace",
        action="store_true",
        help="Run tickets in the main checkouts instead of isolated worktree lanes",
    )
    parser.add_argument(
        "--manual-conflicts",
        action="store_true",
        help="Ask a human to resolve merge conflicts instead of an agent",
    )
    args = parser.parse_args()
    source = read_source(args.source, args.workspace)
    env = observe(
        source, args.python, args.language, not args.shared_workspace, not args.manual_conflicts
    )
    plan = translate(source.document, env)
    report = summary(source, plan, env)
    if args.apply:
        engine = local_engine(args.database.resolve())
        store = engine.store
        catalog = ProjectCatalog(store.catalog(), store.path)
        report["applied"] = apply(engine, catalog, source, plan, args.language)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
