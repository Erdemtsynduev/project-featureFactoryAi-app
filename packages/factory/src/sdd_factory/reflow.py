"""Changing the flow of work in progress, by a person.

A task runs one published version of its flow. A person may change that flow for one
task (skip a step, run a step with another profile, set an option) or move unfinished
work onto the current version of its template after the project's settings or the
templates changed. Either way the run migrates (see `machine.migrate`); started work
keeps its progress where its steps are unchanged.
"""

import time
import uuid
from pathlib import Path

from sdd_core.codec import digest as content_digest
from sdd_core.codec import encode, integer, sequence, text
from sdd_core.editor import flow_changes_of
from sdd_core.models import Json, Run
from sdd_runtime.engine import Engine

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.flows import TEMPLATES, FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import TaskRecord


def template_of(record: TaskRecord) -> str | None:
    """The factory template a task runs, or None when it runs a workflow of its own."""
    if record.intent in TEMPLATES:
        return record.intent
    return record.kind if record.kind in ("feature", "ticket") else None


class FlowChanges:
    def __init__(
        self, engine: Engine, catalog: ProjectCatalog, flows: FlowLibrary, log: FlightLog
    ) -> None:
        self.engine, self.catalog, self.flows, self.log = engine, catalog, flows, log

    def change(self, doc: dict[str, Json]) -> dict[str, object]:
        """Apply flow changes to one task's flow and migrate it onto the new version."""
        run_id = text(doc.get("id"), "id")
        changes = flow_changes_of(doc.get("changes", []))
        run = self.engine.change_flow(
            run_id,
            changes,
            text(doc.get("request_id", uuid.uuid4().hex), "request_id"),
            integer(doc.get("version"), "version"),
            time.time(),
        )
        self.log.record("flow_changed", run=run_id, changes=len(changes))
        return encode(run)

    def current(self, run_id: str) -> str | None:
        """The digest of the current version of the task's template, if it has one."""
        record = self.catalog.task(run_id)
        template = template_of(record)
        if template is None:
            return None
        return self.flows.ensure(
            template, record.project, record.language, self._repositories(run_id, template)
        )

    def update(self, doc: dict[str, Json]) -> dict[str, object]:
        """Move one task onto the current version of its template."""
        run_id = text(doc.get("id"), "id")
        run = self._update(run_id, integer(doc.get("version"), "version"))
        return encode(run)

    def update_many(self, doc: dict[str, Json]) -> dict[str, object]:
        """Move the project's unfinished, idle work onto the current versions of its
        templates. `dry: true` only reports what is outdated."""
        project = text(doc.get("project"), "project")
        wanted = {text(x, "id") for x in sequence(doc.get("ids", []))}
        dry = doc.get("dry") is True
        records = self.catalog.tasks()
        with self.engine.store.unit() as db:
            runs = [run for run in db.runs() if run.status != "accepted"]
        outdated: list[Json] = []
        updated: list[Json] = []
        refused: dict[str, Json] = {}
        for run in runs:
            record = records.get(run.id)
            if record is None or record.project != project or (wanted and run.id not in wanted):
                continue
            try:
                current = self.current(run.id)
                if current is None or current == run.workflow_digest:
                    continue
                outdated.append(run.id)
                if not dry:
                    self._update(run.id, run.version, current)
                    updated.append(run.id)
            except (ValueError, KeyError) as error:
                refused[run.id] = str(error)
        if not dry:
            self.log.record("flows_updated", project=project, updated=updated, refused=refused)
        return {"outdated": outdated, "updated": updated, "refused": refused}

    def _update(self, run_id: str, version: int, current: str | None = None) -> Run:
        digest = current or self.current(run_id)
        if digest is None:
            raise ValueError("This task runs a workflow of its own, not a template")
        run = self.engine.store.get(run_id)
        if run.workflow_digest == digest:
            return run
        request = content_digest(f"flow-update:{run_id}:{version}:{digest}")[:64]
        state = self.engine.migrate(run_id, digest, {}, request, version, time.time())
        self.log.record("flow_updated", run=run_id, digest=digest)
        return state

    def _repositories(self, run_id: str, template: str) -> tuple[str, ...]:
        """The repositories a ticket owns: its claimed folders within its workspace or
        lane. Other templates do not depend on them."""
        if template != "ticket":
            return ()
        with self.engine.store.unit() as db:
            location = db.location(run_id)
        root = Path(location.workspace)
        owned = (Path(path) for path in self.engine.workspace.paths(location.claim))
        return tuple(sorted(path.relative_to(root).as_posix() for path in owned if path != root))
