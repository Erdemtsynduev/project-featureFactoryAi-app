"""Drafts are the intake: an item of a project's sources becomes one draft, once.

A work item (a numbered Markdown plan, a tracker project or issue) is imported as a
paused draft over its open rows. Starting the draft lets the lead cut it into
features; a person approves the cut, and each feature then gets its own specification
and tickets. After the import nothing follows the source: no progress is read from
it and nothing is written to it. Rows added to a source later become a follow-up
draft on the next import.

`collect` imports what no work covers yet. `rebuild` backs the database up and takes
in again everything that never started: never-started drafts, features and tickets
are removed, a parent that lost children is closed and gives its rows back, and the
rows no kept work covers become fresh drafts. Nothing here starts work.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from sdd_core.codec import encode
from sdd_core.models import Json
from sdd_core.tracking import WorkItem, WorkSource
from sdd_runtime.engine import Engine

from sdd_factory.board import replan, source_tickets
from sdd_factory.briefs import draft_brief, source_work
from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import PLANNING_SCOPE, TaskRecord, language_rule, unfinished


class DraftIntake:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        flows: FlowLibrary,
        log: FlightLog,
        sources: Callable[[dict[str, Json]], list[WorkSource]],
    ) -> None:
        """`sources(project)` lists where the project's work items come from."""
        self.engine, self.catalog, self.flows, self.log = engine, catalog, flows, log
        self.sources = sources

    def _project(self, doc: dict[str, Json]) -> tuple[str, dict[str, Json], Path]:
        project_id = str(doc.get("project", ""))
        project = self.catalog.project(project_id)
        if not project:
            raise ValueError("Unknown project")
        return project_id, project, Path(str(project["workspace"])).resolve(strict=True)

    def _items(self, project: dict[str, Json], workspace: Path, only: str) -> list[WorkItem]:
        items = [
            item
            for source in self.sources(project)
            for item in source.items(str(workspace))
            if not only or item.key == only
        ]
        if only and not items:
            raise ValueError(f"No item {only} in the project's sources")
        return items

    # Importing ------------------------------------------------------------------

    def collect(self, doc: dict[str, Json]) -> dict[str, object]:
        """One paused draft per item for the open rows that no work covers yet; an item
        without rows is taken once. `item` narrows the import to one item's key."""
        project_id, project, workspace = self._project(doc)
        language = str(project.get("language", "ru"))
        only = str(doc.get("item", ""))
        items = self._items(project, workspace, only)
        with self.engine.store.unit() as unit:
            status = {run.id: run.status for run in unit.runs()}
        records = {
            key: item
            for key, item in self.catalog.tasks().items()
            if key in status and item.project == project_id
        }
        labels = self.catalog.labels(project_id)
        flow = ""
        created: list[Json] = []
        for item in items:
            taken = [key for key, record in records.items() if record.source == item.source]
            covered = {row for key in taken for row in records[key].rows}
            scope = tuple(row for row in item.rows if row.open and row.id not in covered)
            if not scope and (item.rows or taken):
                continue
            drafts = sum(records[key].kind == "draft" for key in taken)
            identifier = f"draft_{item.key}" + (f"_{drafts + 1}" if drafts else "")
            flow = flow or self.flows.ensure("draft", project_id, language)
            taking = replace(item, rows=scope)
            work = source_work(records, status, source_tickets(records, status, item.source))
            self.engine.create(
                identifier,
                flow,
                workspace,
                language_rule(language) + draft_brief(taking, work, labels),
                None,
                time.time(),
                (),
                (PLANNING_SCOPE,),
            )
            self.catalog.save_task(
                identifier,
                TaskRecord(
                    project=project_id,
                    title=item.title[:200],
                    kind="draft",
                    language=language,
                    rows=tuple(row.id for row in scope),
                    source=item.source,
                    link=item.link,
                ),
            )
            # What was taken is recorded: features are cut from it, not from the source.
            self.catalog.save_artifact(Artifact(identifier, "draft", item.body, encode(taking)))
            created.append(identifier)
        self.log.record(
            "drafts_imported", project=project_id, item=only, items=len(items), created=len(created)
        )
        return {"items": len(items), "created": created}

    # Rebuilding the board ----------------------------------------------------------

    def rebuild(self, doc: dict[str, Json]) -> dict[str, object]:
        """Back up, then take in again all work of the project (or of one `item`) that
        never started.

        Never-started drafts, features and tickets are removed. A parent whose children
        were removed is closed: what it delivered stays, its started children keep
        running as top-level work, its never-started reviews go, and the rows it
        covered are free to be taken in again. Other started work, tasks, reviews and
        anything a kept run depends on stay untouched.
        """
        project_id, project, workspace = self._project(doc)
        only = str(doc.get("item", ""))
        # Refuse before changing anything: the sources must read and drafts be creatable.
        items = self._items(project, workspace, only)
        self.flows.ensure("draft", project_id, str(project.get("language", "ru")))
        with self.engine.store.unit() as unit:
            runs = {run.id: run for run in unit.runs()}
            edges = unit.dependency_edges()
        records = {key: item for key, item in self.catalog.tasks().items() if key in runs}
        sources = frozenset(item.source for item in items) if only else None
        decided = replan(records, runs, edges, project_id, sources)
        path = self.engine.store.path
        backup = path.with_name(f"{path.stem}.before-rebuild-{time.time_ns()}.db")
        self.engine.store.backup(backup)
        removed = self.engine.store.discard(tuple(sorted(decided.removed)))
        status = {key: run.status for key, run in runs.items() if key not in decided.removed}
        kept = {key: item for key, item in records.items() if key not in decided.removed}
        for parent in sorted(decided.reopened):
            self._reopen(parent, kept, status)
        self.log.record(
            "board_rebuilt",
            project=project_id,
            item=only,
            removed=len(removed),
            reopened=list[Json](sorted(decided.reopened)),
            backup=str(backup),
        )
        return {
            "removed": len(removed),
            "reopened": sorted(decided.reopened),
            "backup": str(backup),
            **self.collect(doc),
        }

    def _reopen(
        self, parent: str, kept: Mapping[str, TaskRecord], status: Mapping[str, str]
    ) -> None:
        """Close `parent` with what it delivered and free its rows: neither it nor the
        work it was cut from covers them any more."""
        self.catalog.close_task(parent, unfinished(parent, kept, status))
        freed = set(kept[parent].rows)
        key = parent
        while key in kept and freed:
            record = self.catalog.task(key)
            rows = tuple(row for row in record.rows if row not in freed)
            self.catalog.update_task(key, record.changed(rows=rows))
            key = kept[key].parent
