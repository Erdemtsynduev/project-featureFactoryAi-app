"""Projects, task records and artifacts: the factory's metadata beside the engine.

Everything is stored through the `CatalogRecords` port as canonical JSON. Task
records are typed (`TaskRecord`) and may be corrected after creation; artifacts
(a draft's source item, a specification, a breakdown) are versioned by replacement.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sdd_core.catalog import CatalogRecords
from sdd_core.codec import canonical, mapping, object_json, sequence, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_runtime.lane_model import CommitMessages

from sdd_factory.model import BREAKDOWNS, TaskRecord
from sdd_factory.settings import roles_of
from sdd_factory.trackers import tracker_settings

# A draft's source item, a feature's specification, a parent's approved breakdown,
# and on a plan review the changes it applied.
ARTIFACT_KINDS = ("draft", "specification", *BREAKDOWNS, "plan_changes")


@dataclass(frozen=True)
class Artifact:
    run: str
    kind: str  # one of ARTIFACT_KINDS
    content: str  # Markdown for people
    data: Json = None  # structured form, when there is one


def commit_messages(doc: dict[str, Json]) -> CommitMessages:
    """The project's commit templates for engine commits, checked; defaults when unset."""
    defaults = CommitMessages()
    messages = CommitMessages(
        text(doc.get("commit_message") or defaults.work, "commit_message"),
        text(doc.get("pin_message") or defaults.pin, "pin_message"),
    )
    try:
        messages.check()
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"Commit message template names an unknown field: {error}") from None
    return messages


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
        messages = commit_messages(doc)
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
            # Where the project keeps plan documents imported as drafts; optional.
            "plans_folder": plans_folder,
            # A tracker drafts also come from, and where progress is shown; optional.
            "tracker": tracker_settings(doc.get("tracker")),
            # Tickets run in their own worktree lane; conflicts go to an agent when autonomous.
            "isolation": doc.get("isolation", True) is not False,
            "auto_resolve": doc.get("auto_resolve", True) is not False,
            # Ticket agents may use the internet (web search and fetch).
            "web": doc.get("web", False) is True,
            # The project's commit convention for engine commits in lanes; empty = default.
            "commit_message": messages.work if doc.get("commit_message") else "",
            "pin_message": messages.pin if doc.get("pin_message") else "",
            # The agent profile of each role (lead, analyst, implementer, reviewer) that
            # differs from the templates' default.
            "roles": roles_of(doc.get("roles")),
        }
        self.records.save_project(identifier, canonical(result))
        return result

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

    def labels(self, project: str) -> set[str]:
        """The labels a project's tasks carry: a lead is told to reuse them."""
        return {
            label
            for record in self.tasks().values()
            if record.project == project
            for label in record.labels
        }

    def save_task(self, identifier: str, record: TaskRecord) -> None:
        """Record a new task; an existing record is kept (creation is idempotent)."""
        self.records.save_task(identifier, canonical(record.document()))

    def update_task(self, identifier: str, record: TaskRecord) -> None:
        """Correct a task's record (title, parent, labels) after creation."""
        self.records.update_task(identifier, canonical(record.document()))

    def close_task(self, identifier: str, detached: list[str]) -> list[str]:
        """Close work early: it counts as delivered with what is done, and each of its
        `detached` children becomes top-level work that remembers where it came from."""
        records = self.tasks()
        for key in detached:
            self.update_task(key, records[key].changed(parent="", origin=identifier))
        self.update_task(identifier, records[identifier].changed(closed=True))
        return detached

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

    def breakdown(self, parent: str) -> list[dict[str, Json]]:
        """The approved breakdown of `parent` as stored: each draft with the run it
        became. Empty when nothing was approved."""
        stored = self.artifacts(parent)
        found = next((stored[kind] for kind in BREAKDOWNS if kind in stored), None)
        items = found.data if found is not None and isinstance(found.data, list) else []
        return [item for item in items if isinstance(item, dict)]
