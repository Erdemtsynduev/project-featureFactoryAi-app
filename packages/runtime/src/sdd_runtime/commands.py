"""Run lifecycle commands: creation, operator commands and guidance, recovery routes,
invalidation and lanes. Operator commands are idempotent by request id."""

import re
from collections.abc import Mapping
from pathlib import Path

from sdd_core import machine
from sdd_core.codec import canonical, encode, run_json
from sdd_core.editor import FlowChange, changed_flow
from sdd_core.graph import acyclic, validate
from sdd_core.models import RECORD_ID, Cause, Run
from sdd_core.ports import Conflict

from sdd_runtime.runs import RunContext, replayed

# Room a message leaves in the context for the packet's own framing.
GUIDANCE_MARGIN = 1000


class RunCommands:
    def __init__(self, runs: RunContext) -> None:
        self.runs = runs

    def create(
        self,
        identifier: str,
        definition: str,
        workspace: Path,
        context: str,
        revision: str | None,
        now: float,
        dependencies: tuple[str, ...] = (),
        scope: tuple[str, ...] = (),
    ) -> Run:
        """Create a paused run. `scope` limits ownership and revision to sub-folders.

        Pass `revision=None` to observe the scoped revision now.
        """
        if not re.fullmatch(RECORD_ID, identifier):
            raise ValueError("Invalid run id")
        store = self.runs.store
        workflow = store.workflow(definition)
        if len(context) > workflow.max_input_chars:
            raise ValueError("Context exceeds configured character budget")
        root = self.runs.workspace.resolve(str(workspace))
        claim = self.runs.workspace.claim(root, scope)
        with store.unit() as db:
            policy = db.policy(str(root))
        validate(workflow, policy)
        return store.create(
            Run(identifier, definition, workflow.entry, revision or self.runs.revision_of(claim)),
            str(root),
            context,
            claim,
            now,
            dependencies,
        )

    def command(self, run_id: str, command: str, request_id: str, expected: int, now: float) -> Run:
        request = canonical([run_id, command, expected])
        # A retry past the call limit grants the workflow's budget once more.
        grant = self.runs.workflow_of(run_id).max_calls if command == "retry" else 0
        with self.runs.store.unit() as db:
            done = replayed(db, request_id, request)
            if done is not None:
                return done
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale command version")
            state = db.apply(run, machine.control(run, command, now, grant))
            db.save_command(request_id, request, run_json(state))
            return state

    def message(self, run_id: str, message: str, request_id: str, expected: int, now: float) -> Run:
        """Persist operator guidance for the next packet, never inject into a live process."""
        text = message.strip()
        if not text:
            raise ValueError("Message cannot be empty")
        workflow = self.runs.workflow_of(run_id)
        request = canonical([run_id, "message", expected, message])
        with self.runs.store.unit() as db:
            done = replayed(db, request_id, request)
            if done is not None:
                return done
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale message; refresh the task")
            transition = machine.guidance(run, now, text)
            context = db.context(run_id) + "\nOperator guidance:\n" + text
            if len(context) > workflow.max_input_chars - GUIDANCE_MARGIN:
                raise ValueError("Message exceeds context budget; use a smaller instruction")
            db.set_context(run_id, context)
            state = db.apply(run, transition)
            db.save_command(request_id, request, run_json(state))
            return state

    def revise(
        self,
        run_id: str,
        context: str,
        claim: str,
        dependencies: tuple[str, ...],
        request_id: str,
        expected: int,
        now: float,
    ) -> Run:
        """Rewrite a never-started run's brief, owned paths and prerequisites (an approved
        plan review). Idempotent by request id; a cycle or a started run is refused."""
        workflow = self.runs.workflow_of(run_id)
        if len(context) > workflow.max_input_chars:
            raise ValueError("Context exceeds configured character budget")
        request = canonical([run_id, "revise", expected, context, claim, sorted(dependencies)])
        with self.runs.store.unit() as db:
            done = replayed(db, request_id, request)
            if done is not None:
                return done
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale plan revision; refresh the task")
            transition = machine.plan_revised(run, now, canonical(sorted(dependencies)))
            kept = [edge for edge in db.dependency_edges() if edge[0] != run_id]
            acyclic([*kept, *((run_id, p) for p in dependencies)], "Dependencies form a cycle")
            db.set_context(run_id, context)
            db.relocate(run_id, db.location(run_id).workspace, claim)
            db.set_dependencies(run_id, dependencies)
            state = db.apply(run, transition)
            db.save_command(request_id, request, run_json(state))
            return state

    def change_flow(
        self,
        run_id: str,
        changes: tuple[FlowChange, ...],
        request_id: str,
        expected: int,
        now: float,
    ) -> Run:
        """Publish the run's workflow with `changes` as a new version and migrate the run
        onto it (see `machine.migrate`). Idempotent by request id."""
        request = canonical(
            [run_id, "change_flow", expected, [[type(c).__name__, encode(c)] for c in changes]]
        )
        with self.runs.store.unit() as db:
            done = replayed(db, request_id, request)
            if done is not None:
                return done
        new, moved = changed_flow(self.runs.workflow_of(run_id), changes)
        digest = self.runs.store.publish(new)
        return self._migrate(run_id, digest, moved, request_id, request, expected, now)

    def migrate(
        self,
        run_id: str,
        digest: str,
        moved: Mapping[str, str],
        request_id: str,
        expected: int,
        now: float,
    ) -> Run:
        """Move an idle run onto the published workflow `digest`. Idempotent by request id."""
        request = canonical([run_id, "migrate", expected, digest, sorted(moved.items())])
        return self._migrate(run_id, digest, moved, request_id, request, expected, now)

    def _migrate(
        self,
        run_id: str,
        digest: str,
        moved: Mapping[str, str],
        request_id: str,
        request: str,
        expected: int,
        now: float,
    ) -> Run:
        """The project's mandatory gates still hold; a live attempt or a stale version is
        refused."""
        new = self.runs.flow(digest)
        with self.runs.store.unit() as db:
            done = replayed(db, request_id, request)
            if done is not None:
                return done
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale flow change; refresh the task")
            validate(new, db.policy(db.location(run_id).workspace))
            old = self.runs.flow(run.workflow_digest)
            state = db.apply(run, machine.migrate(run, old, new, digest, now, moved))
            db.save_command(request_id, request, run_json(state))
            return state

    def request_recovery(self, run_id: str, expected: int, now: float) -> Run:
        """Select the workflow's declared reconciliation path without accepting work.

        When no mutating attempt has changed anything yet, there is nothing to
        reconcile: the run goes back to its first mutating step instead.
        """
        workflow = self.runs.workflow_of(run_id)
        observed = self.runs.observe(run_id)
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale recovery request")
            if not machine.restartable(run):
                raise ValueError("Recovery requires an inactive unfinished task")
            first = machine.first_untouched(
                workflow, db.attempt_bases(run_id), db.step_results(run_id), observed
            )
            if first is not None and first != run.step:
                return db.apply(run, machine.restart(run, first, observed, now))
            if not machine.reconcilable(run):
                raise ValueError("Recovery requires an inactive blocked or waiting task")
            target = machine.recovery_target(workflow, run.step)
            if target is None:
                raise ValueError("Workflow has no read-only recovery path")
            return db.apply(run, machine.reconcile(run, target, observed, now))

    def relocate(self, run_id: str, workspace: str, claim: str, now: float) -> Run:
        """Move an idle run into its isolated working copy.

        The claim and revision switch in one transaction, so the lane is never
        mistaken for an outside change; stale gates are cleared.
        """
        observed = self.runs.revision_of(claim)
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            if run.active is not None:
                raise Conflict("Cannot move an active attempt")
            db.relocate(run_id, workspace, claim)
            return db.apply(run, machine.relocate(run, observed, now, workspace))

    def invalidate(self, run_id: str, revision: str, now: float) -> Run:
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            if run.active:
                raise Conflict("Cannot invalidate an active attempt")
            return db.apply(run, machine.invalidate(run, revision, now))

    def rebase_revision(self, run_id: str, revision: str, now: float) -> Run:
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            if run.active:
                raise Conflict("Cannot re-base an active attempt")
            return db.apply(run, machine.rebase_revision(run, revision, now))

    def block(self, run_id: str, now: float, reason: str, cause: Cause = "blocked") -> Run:
        """Park one run with a visible reason; other runs keep being scheduled."""
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            return db.apply(run, machine.block(run, now, reason, cause=cause))
