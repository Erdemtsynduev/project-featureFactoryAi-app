"""Operator control of runs: one command, the same command for many, messages and recovery."""

import time
import uuid

from sdd_core.codec import encode, flag, integer, sequence, text
from sdd_core.models import Json, Run
from sdd_core.ports import Conflict
from sdd_runtime.engine import Engine

from sdd_factory.board import below
from sdd_factory.catalog import ProjectCatalog
from sdd_factory.diagnostics import record
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    TaskRecord,
    ticket_places,
)


class WorkControl:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        log: FlightLog,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.log = log

    def command(self, command: str, doc: dict[str, Json]) -> dict[str, object]:
        run = self.engine.command(
            text(doc.get("id"), "id"),
            command,
            text(doc.get("request_id", uuid.uuid4().hex), "request_id"),
            integer(doc.get("version"), "version"),
            time.time(),
        )
        self.log.record("command", run=run.id, command=command, status=run.status)
        return encode(run)

    def bulk(self, command: str, doc: dict[str, Json]) -> dict[str, object]:
        """Resume or pause many tasks of one project in one operator action.

        `scope` "startable" resumes paused tasks whose dependencies are accepted;
        "all" resumes every paused task (the rest wait for their dependencies).
        Optional `kind`, `label`, `under` and `ids` narrow it to what the operator
        selected (the board filter, everything cut from one task, one task). `with_dependencies` also resumes
        the paused prerequisites of every unfinished selected task, transitively
        within the project, so work that waits on other work moves.
        Each task is commanded at its own current version; moved tasks are skipped.
        A ticket its breakdown marks HITL resumes only when named in `ids`: a person
        takes part in it, so it never starts as part of a whole feature.
        """
        if command not in ("resume", "pause"):
            raise ValueError("Bulk command must be resume or pause")
        project_id = text(doc.get("project", ""), "project")
        scope = text(doc.get("scope", "startable"), "scope")
        if scope not in ("startable", "all"):
            raise ValueError("Unknown scope")
        kind = text(doc.get("kind", ""), "kind")
        label = text(doc.get("label", ""), "label")
        under = text(doc.get("under", ""), "under")
        ids = {text(x, "id") for x in sequence(doc.get("ids", []))}
        with_dependencies = flag(doc.get("with_dependencies", False), "with_dependencies")
        records = self.catalog.tasks()
        inside = below(records, under) if under else set()

        def wanted(run: Run) -> bool:
            item = records.get(run.id, TaskRecord())
            return (
                (not kind or item.kind == TaskRecord.load({"kind": kind}).kind)
                and (not label or label in item.labels)
                and (not under or run.id in inside)
                and (not ids or run.id in ids)
            )

        project = self.project_runs(project_id)
        selected = [run for run in project if wanted(run)]
        chosen = [run for run in selected if self._eligible(command, run, scope)]
        if command == "resume" and with_dependencies:
            unfinished = [run for run in selected if run.status != "accepted"]
            chosen += self._prerequisites(unfinished, {run.id for run in project})
        held: list[str] = []
        if command == "resume":
            people = self.hitl(records)
            held = [run.id for run in chosen if run.id in people and run.id not in ids]
            chosen = [run for run in chosen if run.id not in held]
        done: list[str] = []
        skipped = 0
        for run in chosen:
            try:
                self.engine.command(run.id, command, uuid.uuid4().hex, run.version, time.time())
                done.append(run.id)
            except (ValueError, Conflict):
                skipped += 1
        self.log.record(
            f"bulk_{command}",
            project=project_id,
            scope=scope,
            task_kind=kind,
            label=label,
            under=under,
            tasks=len(ids),
            with_dependencies=with_dependencies,
            count=len(done),
            held=list[Json](held),
        )
        return {"changed": list[Json](done), "skipped": skipped, "held": list[Json](held)}

    def hitl(self, records: dict[str, TaskRecord]) -> frozenset[str]:
        """Tickets whose approved breakdown says a person must take part in them."""
        found: set[str] = set()
        for parent in {item.parent for item in records.values() if item.parent}:
            places = ticket_places(list[Json](self.catalog.breakdown(parent)))
            found.update(run for run, place in places.items() if place["hitl"] is True)
        return frozenset(found)

    def _prerequisites(self, selected: list[Run], project: set[str]) -> list[Run]:
        """Resumable prerequisites of `selected` outside it, within the project."""
        seen = {run.id for run in selected}
        found: list[Run] = []
        frontier = list(selected)
        with self.engine.store.unit() as db:
            while frontier:
                for dependency in db.dependencies(frontier.pop().id):
                    if dependency.id in seen or dependency.id not in project:
                        continue
                    seen.add(dependency.id)
                    frontier.append(dependency)
                    if self._eligible("resume", dependency, "all"):
                        found.append(dependency)
        return found

    def _eligible(self, command: str, run: Run, scope: str) -> bool:
        if run.status == "accepted" or run.active is not None:
            return False
        if command == "pause":
            return not run.paused
        if not run.paused or run.status == "blocked":
            return False
        if scope == "all":
            return True
        with self.engine.store.unit() as db:
            return all(dependency.status == "accepted" for dependency in db.dependencies(run.id))

    def project_runs(self, project_id: str) -> list[Run]:
        """Runs of a project: by their record, else by workspace (older imports)."""
        project = self.catalog.project(project_id)
        records = self.catalog.tasks()
        workspaces = {str(p["id"]): str(p["workspace"]) for p in self.catalog.projects()}
        with self.engine.store.unit() as db:
            runs = db.runs()
            locations = dict(db.locations())

        def owner(run: Run) -> str:
            declared = records.get(run.id, TaskRecord()).project
            if declared in workspaces:
                return declared
            return next((k for k, v in workspaces.items() if v == locations[run.id]), "")

        if project_id and not project:
            raise ValueError("Unknown project")
        return [run for run in runs if owner(run) == project_id]

    def message(self, doc: dict[str, Json]) -> dict[str, object]:
        return encode(
            self.engine.message(
                text(doc.get("id"), "id"),
                text(doc.get("message"), "message"),
                text(doc.get("request_id"), "request_id"),
                integer(doc.get("version"), "version"),
                time.time(),
            )
        )

    def recover(self, doc: dict[str, Json]) -> dict[str, object]:
        """Hand the incident to the read-only recovery step, then leave the task paused."""
        run_id = text(doc.get("id"), "id")
        version = integer(doc.get("version"), "version")
        briefing = record(self.engine, self.log, run_id).briefing()
        guided = self.engine.message(run_id, briefing, uuid.uuid4().hex, version, time.time())
        state = self.engine.request_recovery(run_id, guided.version, time.time())
        self.log.record("recovery_requested", run=run_id, step=state.step)
        return encode(state)
