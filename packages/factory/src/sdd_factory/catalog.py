"""Projects, plans, task records and artifacts: the factory's metadata beside the engine.

Everything is stored through the `CatalogRecords` port as canonical JSON. Task
records are typed (`TaskRecord`) and may be corrected after creation; artifacts
(a feature's specification and ticket breakdown) are versioned by replacement.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sdd_core.catalog import CatalogRecords
from sdd_core.codec import canonical, mapping, object_json, sequence, text
from sdd_core.models import Json
from sdd_core.ports import Conflict

from sdd_factory.model import TaskRecord
from sdd_factory.trackers import tracker_settings

ARTIFACT_KINDS = ("specification", "tickets")


@dataclass(frozen=True)
class Artifact:
    run: str
    kind: str  # specification | tickets
    content: str  # Markdown for people
    data: Json = None  # structured form, when there is one


def checks_of(value: Json, label: str) -> list[str]:
    """A check command line: its program must be an existing absolute executable."""
    argv = [text(arg, label) for arg in sequence(value or [])]
    if argv and (not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()):
        raise ValueError("Checks require an existing absolute executable")
    return argv


class ProjectCatalog:
    def __init__(self, records: CatalogRecords, database: Path) -> None:
        """`database` is the control database, kept outside every project workspace."""
        self.records = records
        self.database = database.resolve()

    # Projects ------------------------------------------------------------------

    def projects(self) -> list[dict[str, Json]]:
        return [object_json(document) for document in self.records.projects()]

    def project(self, identifier: str) -> dict[str, Json]:
        return next((p for p in self.projects() if p["id"] == identifier), {})

    def save_project(self, doc: dict[str, Json]) -> dict[str, Json]:
        identifier, name = text(doc.get("id"), "id"), text(doc.get("name"), "name").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) or not name:
            raise ValueError("Project needs a valid ID and name")
        workspace = Path(text(doc.get("workspace"), "workspace")).resolve(strict=True)
        if not workspace.is_dir() or self.database.is_relative_to(workspace):
            raise ValueError("Project must be a folder outside the control database")
        language = text(doc.get("language", "ru"), "language")
        if language not in ("ru", "en"):
            raise ValueError("Unsupported response language")
        checks = checks_of(doc.get("checks", []), "check argument")
        # Per-repository checks: a ticket runs the checks of the repositories it owns.
        repository_checks: dict[str, Json] = {}
        for repository, argv in mapping(doc.get("repository_checks", {})).items():
            folder = (workspace / repository).resolve()
            if not folder.is_relative_to(workspace) or not folder.is_dir():
                raise ValueError(f"Repository for checks is not a folder: {repository}")
            repository_checks[Path(repository).as_posix()] = list[Json](
                checks_of(argv, "repository check")
            )
        plans_folder = text(doc.get("plans_folder", ""), "plans_folder").strip().strip("/\\")
        if plans_folder:
            folder = (workspace / plans_folder).resolve()
            if not folder.is_relative_to(workspace) or not folder.is_dir():
                raise ValueError(f"Plans folder is not a folder in the workspace: {plans_folder}")
            plans_folder = folder.relative_to(workspace).as_posix()
        for existing in self.projects():
            if existing["id"] != identifier and Path(str(existing["workspace"])) == workspace:
                raise Conflict("This workspace is already registered")
            if existing["id"] == identifier and Path(str(existing["workspace"])) != workspace:
                raise Conflict("Project workspace is immutable; add another project")
        result: dict[str, Json] = {
            "id": identifier,
            "name": name,
            "workspace": str(workspace),
            "language": language,
            "checks": list[Json](checks),
            "repository_checks": repository_checks,
            # Where the project keeps plan documents that become features; optional.
            "plans_folder": plans_folder,
            # Where work comes from and where progress is shown, when not Markdown plans.
            "tracker": tracker_settings(doc.get("tracker")),
            # Tickets run in their own worktree lane; conflicts go to an agent when autonomous.
            "isolation": doc.get("isolation", True) is not False,
            "auto_resolve": doc.get("auto_resolve", True) is not False,
        }
        self.records.save_project(identifier, canonical(result))
        return result

    # Plans ---------------------------------------------------------------------

    def save_plans(self, project: str, plans: list[dict[str, Json]]) -> None:
        """Plan summaries shown on the board; they are metadata, not runs."""
        self.records.save_plans(
            project, tuple((text(plan.get("id"), "id"), canonical(plan)) for plan in plans)
        )

    def plans(self) -> dict[str, list[dict[str, Json]]]:
        found: dict[str, list[dict[str, Json]]] = defaultdict(list)
        for project, document in self.records.plans():
            found[project].append(object_json(document))
        return dict(found)

    # Task records ----------------------------------------------------------------

    def tasks(self) -> dict[str, TaskRecord]:
        return {
            identifier: TaskRecord.load(object_json(document))
            for identifier, document in self.records.tasks()
        }

    def task(self, identifier: str) -> TaskRecord:
        return self.tasks().get(identifier, TaskRecord())

    def task_metadata(self) -> dict[str, dict[str, Json]]:
        """Task records as served to the UI, in their current spelling."""
        return {identifier: record.document() for identifier, record in self.tasks().items()}

    def save_task(self, identifier: str, record: TaskRecord) -> None:
        """Record a new task; an existing record is kept (creation is idempotent)."""
        self.records.save_task(identifier, canonical(record.document()))

    def update_task(self, identifier: str, record: TaskRecord) -> None:
        """Correct a task's record (title, plan, parent) after creation."""
        self.records.update_task(identifier, canonical(record.document()))

    # Artifacts -----------------------------------------------------------------

    def save_artifact(self, artifact: Artifact) -> None:
        if artifact.kind not in ARTIFACT_KINDS:
            raise ValueError(f"Unknown artifact kind: {artifact.kind}")
        self.records.save_artifact(
            artifact.run,
            artifact.kind,
            canonical({"content": artifact.content, "data": artifact.data}),
        )

    def artifacts(self, run: str) -> dict[str, Artifact]:
        found: dict[str, Artifact] = {}
        for kind, document in self.records.artifacts(run):
            value = object_json(document)
            found[kind] = Artifact(run, kind, str(value.get("content", "")), value.get("data"))
        return found
