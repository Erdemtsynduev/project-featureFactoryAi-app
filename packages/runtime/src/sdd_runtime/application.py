"""Transactional orchestration service. UI and workers are command clients."""

import re
from pathlib import Path

from sdd_core import machine, questions
from sdd_core.codec import (
    canonical,
    digest,
    object_json,
    result_json,
    run_json,
    run_load,
)
from sdd_core.graph import validate
from sdd_core.models import Result, Run
from sdd_core.ports import Conflict, StaleVersion, StateStore, Workspace
from sdd_core.records import AdmissionRecords
from sdd_core.sdk import ProjectAdapter

# Slow observations (Git revision, evidence hashes) run outside transactions; the
# write then compares-and-swaps the run version and retries if it moved meanwhile.
RETRIES = 3


class ApplicationEngine:
    def __init__(
        self,
        store: StateStore,
        project: ProjectAdapter,
        workspace: Workspace,
        max_queue_calls: int | None = None,
        max_queue_planning_calls: int | None = None,
        max_agents: int = 2,
        max_operations: int = 1,
    ) -> None:
        if any(
            value is not None and value < 0 for value in (max_queue_calls, max_queue_planning_calls)
        ):
            raise ValueError("Queue call budgets cannot be negative")
        if max_agents < 1 or max_operations < 1:
            raise ValueError("At least one agent and one operation slot are required")
        self.store = store
        self.project = project
        self.workspace = workspace
        self.max_queue_calls = max_queue_calls
        self.max_queue_planning_calls = max_queue_planning_calls
        self.max_agents = max_agents
        self.max_operations = max_operations

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
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
            raise ValueError("Invalid run id")
        workflow = self.store.workflow(definition)
        if len(context) > workflow.max_input_chars:
            raise ValueError("Context exceeds configured character budget")
        root = self.workspace.resolve(str(workspace))
        claim = self.workspace.claim(root, scope)
        with self.store.unit() as db:
            policy = db.policy(str(root))
        validate(workflow, policy)
        return self.store.create(
            Run(identifier, definition, workflow.entry, revision or self.revision_of(claim)),
            str(root),
            context,
            claim,
            now,
            dependencies,
        )

    def revision_of(self, claim: str) -> str:
        paths = self.workspace.paths(claim)
        if len(paths) == 1:
            return self.project.revision(paths[0])
        return digest(canonical([[path, self.project.revision(path)] for path in paths]))

    def observe(self, run_id: str) -> str:
        """Current revision of everything the run owns."""
        with self.store.unit() as db:
            claim = db.location(run_id)[1]
        return self.revision_of(claim)

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

    def _admitted(self, db: AdmissionRecords, run_id: str, kind: str, claim: str) -> bool:
        if any(dep.status != "accepted" for dep in db.dependencies(run_id)):
            return False
        active = db.active_claims()
        category = "agent" if kind == "agent" else "operation"
        count = sum(("agent" if row[0] == "agent" else "operation") == category for row in active)
        if count >= (self.max_agents if category == "agent" else self.max_operations):
            return False
        if any(self.workspace.overlaps(claim, other) for other in db.unfinished_claims(run_id)):
            return False
        return not any(self.workspace.overlaps(claim, other) for _, other in active)

    def _over_budget(self, calls: int, planning_calls: int, planning: bool) -> bool:
        return (self.max_queue_calls is not None and calls >= self.max_queue_calls) or (
            planning
            and self.max_queue_planning_calls is not None
            and planning_calls >= self.max_queue_planning_calls
        )

    def dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        for _ in range(RETRIES):
            try:
                return self._dispatch(run_id, now, attempt_id)
            except StaleVersion:
                continue
        raise Conflict("Task kept changing during dispatch")

    def _dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        with self.store.unit() as db:
            run = db.run(run_id)
            root, claim = db.location(run_id)
            results = db.results(run_id)
        workflow = self.store.workflow(run.workflow_digest)
        step = workflow.step(run.step)
        kind = step.kind
        facts = "{}"
        if kind == "condition" and run.previous_attempt is not None:
            previous = run.previous_attempt
            facts = next((r.data for r in results if r.attempt_id == previous), "{}")
        transition = machine.dispatch(run, workflow, now, attempt_id, facts)
        if kind == "finish" and run.gates:
            if self.revision_of(claim) != run.revision:
                raise ValueError("Acceptance revision changed")
            for result in results:
                if result.revision == run.revision and result.outcome == "passed":
                    self.workspace.verify(result, root, run.revision)
        with self.store.unit() as db:
            if kind == "agent" and transition.effects:
                planning = object_json(step.config).get("purpose") == "planning"
                if self._over_budget(*db.queue_usage(), planning):
                    return db.apply(
                        run,
                        machine.block(
                            run, now, "Queue call budget exhausted", event="limit", detail=""
                        ),
                    )
            if kind != "condition" and not self._admitted(db, run_id, kind, claim):
                raise Conflict("Waiting for dependency or resource ownership")
            # CAS against the snapshot: the observations above belong to this version.
            return db.apply(run, transition)

    def _normalize(self, run_id: str, result: Result) -> Result:
        if not result.artifacts:
            return result
        with self.store.unit() as db:
            workspace = db.location(run_id)[0]
        return self.workspace.normalize(run_id, result, workspace)

    def complete(self, run_id: str, result: Result, now: float, expected: int | None = None) -> Run:
        """Apply a result once. `expected` pins the run version an operator answered."""
        result = self._normalize(run_id, result)
        document = result_json(result)
        fingerprint = digest(document)
        for _ in range(RETRIES):
            with self.store.unit() as db:
                old = db.receipt(result.attempt_id)
                if old:
                    if old != (fingerprint, run_id):
                        raise Conflict("Conflicting repeated result")
                    return db.run(run_id)
                run = db.run(run_id)
                root, claim = db.location(run_id)
            if expected is not None and run.version != expected:
                raise Conflict("Stale answer; refresh the task")
            if result.artifacts:
                self.workspace.verify(result, root, result.revision)
                if self.revision_of(claim) != result.revision:
                    raise ValueError("Result no longer matches workspace")
            transition = machine.complete(
                run, self.store.workflow(run.workflow_digest), result, now
            )
            try:
                with self.store.unit() as db:
                    old = db.receipt(result.attempt_id)
                    if old:
                        if old != (fingerprint, run_id):
                            raise Conflict("Conflicting repeated result")
                        return db.run(run_id)
                    state = db.apply(run, transition)
                    db.save_result(result.attempt_id, fingerprint, document)
                    return state
            except StaleVersion:
                if expected is not None:
                    raise Conflict("Stale answer; refresh the task") from None
        raise Conflict("Task kept changing while its result was applied")

    def recover(self, run_id: str, now: float, confirmed: bool, reason: str, revision: str) -> Run:
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        with self.store.unit() as db:
            run = db.run(run_id)
            transition = machine.recover(
                run, now, termination_confirmed=confirmed, reason=reason, observed_revision=revision
            )
            target = object_json(workflow.step(run.step).config).get("recovery_step")
            if confirmed and target and transition.state.status == "waiting":
                if not isinstance(target, str) or workflow.step(target).mutates:
                    raise ValueError("Recovery requires a read-only step")
                transition = machine.recover(
                    run,
                    now,
                    termination_confirmed=confirmed,
                    reason=reason,
                    observed_revision=revision,
                    recovery_step=target,
                )
            state = db.apply(run, transition)
            if run.active:
                db.effect_status(run.active.id, "abandoned" if confirmed else "uncertain")
            return state

    def relocate(self, run_id: str, workspace: str, claim: str, now: float) -> Run:
        """Move an idle run into its isolated working copy.

        The claim and revision switch in one transaction, so the lane is never
        mistaken for an outside change; stale gates are cleared.
        """
        observed = self.revision_of(claim)
        with self.store.unit() as db:
            run = db.run(run_id)
            if run.active is not None:
                raise Conflict("Cannot move an active attempt")
            db.relocate(run_id, workspace, claim)
            return db.apply(run, machine.relocate(run, observed, now, workspace))

    def facts(self, run_id: str) -> str:
        """Data of the result that led to the current step (the agent's questions)."""
        with self.store.unit() as db:
            run = db.run(run_id)
            previous = run.active.previous if run.active else run.previous_attempt
            if previous is None:
                return "{}"
            return next((r.data for r in db.results(run_id) if r.attempt_id == previous), "{}")

    def asked(self, run_id: str) -> str:
        """Questions for the waiting human step: the agent's, else the step's own."""
        facts = self.facts(run_id)
        if questions.questions(facts):
            return facts
        run = self.store.get(run_id)
        if run.active is None:
            return "{}"
        preset = object_json(self.store.workflow(run.workflow_digest).step(run.active.step).config)
        return canonical({"questions": preset.get("questions", [])})

    def answer(
        self,
        run_id: str,
        outcome: str,
        reply: str,
        choices: dict[str, str],
        expected: int,
        now: float,
    ) -> Run:
        """Resolve a waiting human step with chosen options and/or free text."""
        run = self.store.get(run_id)
        workflow = self.store.workflow(run.workflow_digest)
        if run.version != expected:
            raise Conflict("Stale answer; refresh the task")
        if run.active is None or workflow.step(run.active.step).kind != "human":
            raise ValueError("Not waiting for a human")
        asked = questions.questions(self.asked(run_id)) if choices else ()
        if choices and not {key for key in choices} <= {q.id for q in asked}:
            raise ValueError("Answer refers to an unknown question")
        text, data = questions.answer(asked, choices, reply)
        result = Result(
            run.active.id, run.active.generation, outcome, text, run.revision, data=data
        )
        return self.complete(run_id, result, now, expected)

    def auto_answer(self, run_id: str, now: float) -> Run | None:
        """Answer a waiting human step with the agent's recommendations when allowed.

        Allowed by the task's operator switch or the step's `auto_answer:
        recommended` policy, and only when every question has a recommendation.
        """
        run = self.store.get(run_id)
        if run.active is None:
            return None
        step = self.store.workflow(run.workflow_digest).step(run.active.step)
        settings = object_json(step.config)
        if step.kind != "human" or not (
            run.auto_answer or settings.get("auto_answer") == "recommended"
        ):
            return None
        try:
            decision = questions.recommended(self.asked(run_id))
        except ValueError:
            return None
        if decision is None:
            return None
        outcome = settings.get("auto_outcome", step.transitions[0][0])
        if not isinstance(outcome, str):
            raise ValueError("auto_outcome must be text")
        text, data = decision
        result = Result(
            run.active.id, run.active.generation, outcome, text, run.revision, data=data
        )
        return self.complete(run_id, result, now)

    def release_condition(self, run_id: str, now: float) -> Run:
        """Return a condition attempt persisted by an earlier release to pure routing."""
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        with self.store.unit() as db:
            run = db.run(run_id)
            if run.active is None or workflow.step(run.active.step).kind != "condition":
                raise ValueError("No persisted condition attempt")
            state = db.apply(run, machine.release_condition(run, now))
            db.effect_status(run.active.id, "abandoned")
            return state

    def message(self, run_id: str, message: str, request_id: str, expected: int, now: float) -> Run:
        """Persist operator guidance for the next packet, never inject into a live process."""
        if not message.strip():
            raise ValueError("Message cannot be empty")
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        request = canonical([run_id, "message", expected, message])
        with self.store.unit() as db:
            old = db.command(request_id)
            if old:
                if old[0] != request:
                    raise Conflict("Command id reused")
                return run_load(old[1])
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale message; refresh the task")
            transition = machine.guidance(run, now, message.strip())
            context = db.context(run_id) + "\nOperator guidance:\n" + message.strip()
            if len(context) > workflow.max_input_chars - 1000:
                raise ValueError("Message exceeds context budget; use a smaller instruction")
            db.set_context(run_id, context)
            state = db.apply(run, transition)
            db.save_command(request_id, request, run_json(state))
            return state

    def request_recovery(self, run_id: str, expected: int, now: float) -> Run:
        """Select the workflow's declared reconciliation path without accepting work."""
        workflow = self.store.workflow(self.store.get(run_id).workflow_digest)
        observed = self.observe(run_id)
        with self.store.unit() as db:
            run = db.run(run_id)
            if run.version != expected:
                raise Conflict("Stale recovery request")
            if run.active or run.status not in ("blocked", "waiting"):
                raise ValueError("Recovery requires an inactive blocked or waiting task")
            target = object_json(workflow.step(run.step).config).get("recovery_step")
            if not isinstance(target, str) or workflow.step(target).mutates:
                raise ValueError("Workflow has no read-only recovery path")
            return db.apply(run, machine.reconcile(run, target, observed, now))

    def invalidate(self, run_id: str, revision: str, now: float) -> Run:
        with self.store.unit() as db:
            run = db.run(run_id)
            if run.active:
                raise Conflict("Cannot invalidate an active attempt")
            return db.apply(run, machine.invalidate(run, revision, now))

    def block(self, run_id: str, now: float, reason: str) -> Run:
        """Park one run with a visible reason; other runs keep being scheduled."""
        with self.store.unit() as db:
            run = db.run(run_id)
            return db.apply(run, machine.block(run, now, reason))
