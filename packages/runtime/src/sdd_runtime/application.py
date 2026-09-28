"""Transactional orchestration service. UI and workers are command clients."""

import re
from dataclasses import replace
from pathlib import Path

from sdd_core import machine
from sdd_core.codec import (
    canonical,
    digest,
    result_json,
    run_json,
    run_load,
)
from sdd_core.graph import validate
from sdd_core.models import Result, Run
from sdd_core.ports import Conflict, StateStore, UnitOfWork, Workspace
from sdd_core.sdk import ProjectAdapter


class ApplicationEngine:
    def __init__(self, store: StateStore, project: ProjectAdapter, workspace: Workspace) -> None:
        self.store = store
        self.project = project
        self.workspace = workspace

    def create(
        self,
        identifier: str,
        definition: str,
        workspace: Path,
        context: str,
        revision: str,
        now: float,
        dependencies: tuple[str, ...] = (),
    ) -> Run:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
            raise ValueError("Invalid run id")
        workflow = self.store.workflow(definition)
        if len(context) > workflow.max_input_chars:
            raise ValueError("Context exceeds configured character budget")
        root = self.workspace.resolve(str(workspace))
        with self.store.unit() as db:
            policy = db.policy(str(root))
        validate(workflow, policy)
        return self.store.create(
            Run(identifier, definition, workflow.entry, revision),
            str(root),
            context,
            str(root),
            now,
            dependencies,
        )

    def command(self, run_id: str, command: str, request_id: str, expected: int, now: float) -> Run:
        request = canonical([run_id, command, expected])
        with self.store.unit() as db:
            old = db.command(request_id)
            if old:
                if old[0] != request:
                    raise Conflict("Command id reused")
                return run_load(str(old[1]))
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale command version")
            state = db.apply(run, machine.control(run, command, now))
            db.save_command(request_id, request, run_json(state))
            return state

    def _admitted(self, db: UnitOfWork, run: Run, kind: str) -> bool:
        if any(dep.status != "accepted" for dep in db.dependencies(run.id)):
            return False
        active = db.active_claims()
        category = "agent" if kind == "agent" else "operation"
        count = sum(("agent" if row[0] == "agent" else "operation") == category for row in active)
        if count >= (2 if category == "agent" else 1):
            return False
        claim = db.location(run.id)[1]
        if any(self.workspace.overlaps(claim, other) for other in db.unfinished_claims(run.id)):
            return False
        return not any(self.workspace.overlaps(claim, other) for _, other in active)

    def dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        with self.store.unit() as db:
            run = db.run(run_id)
            kind = workflow.step(run.step).kind
            if kind == "finish" and run.gates:
                root = db.location(run_id)[0]
                if self.project.revision(str(root)) != run.revision:
                    raise ValueError("Acceptance revision changed")
                for result in db.results(run_id):
                    if result.revision == run.revision and result.outcome == "passed":
                        self.workspace.verify(result, root, run.revision)
            if not self._admitted(db, run, kind):
                raise Conflict("Waiting for dependency or resource ownership")
            return db.apply(run, machine.dispatch(run, workflow, now, attempt_id))

    def _normalize(self, run_id: str, result: Result) -> Result:
        if not result.artifacts:
            return result
        with self.store.unit() as db:
            workspace = db.location(run_id)[0]
        return self.workspace.normalize(run_id, result, workspace)

    def complete(self, run_id: str, result: Result, now: float) -> Run:
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        result = self._normalize(run_id, result)
        document = result_json(result)
        fingerprint = digest(document)
        with self.store.unit() as db:
            old = db.receipt(result.attempt_id)
            if old:
                if old != (fingerprint, run_id):
                    raise Conflict("Conflicting repeated result")
                return db.run(run_id)
            run = db.run(run_id)
            root = db.location(run_id)[0]
            if result.artifacts:
                self.workspace.verify(result, root, result.revision)
                if self.project.revision(str(root)) != result.revision:
                    raise ValueError("Result no longer matches workspace")
            state = db.apply(run, machine.complete(run, workflow, result, now))
            db.save_result(result.attempt_id, fingerprint, document)
            return state

    def recover(self, run_id: str, now: float, confirmed: bool, reason: str, revision: str) -> Run:
        with self.store.unit() as db:
            run = db.run(run_id)
            transition = machine.recover(
                run, now, termination_confirmed=confirmed, reason=reason, observed_revision=revision
            )
            state = db.apply(run, transition)
            if run.active:
                db.effect_status(run.active.id, "abandoned" if confirmed else "uncertain")
            return state

    def invalidate(self, run_id: str, revision: str, now: float) -> Run:
        with self.store.unit() as db:
            run = db.run(run_id)
            if run.active:
                raise Conflict("Cannot invalidate an active attempt")
            return db.apply(
                run,
                machine.changed(
                    replace(
                        run,
                        revision=revision,
                        gates=(),
                        status="blocked",
                        reason="Workspace changed outside attempt",
                    ),
                    now,
                    "revision_changed",
                ),
            )
