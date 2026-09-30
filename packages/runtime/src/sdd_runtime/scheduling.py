"""Dispatch: whether a run may start its step now, and starting it."""

from sdd_core import machine
from sdd_core.admission import QueueBudget, Slots
from sdd_core.models import PROCESS_KINDS, Run
from sdd_core.ports import Conflict
from sdd_core.records import AdmissionRecords

from sdd_runtime.runs import RunContext, data_of, optimistic


class Scheduler:
    def __init__(self, runs: RunContext, slots: Slots, budget: QueueBudget) -> None:
        self.runs = runs
        self.slots = slots
        self.budget = budget

    def _admitted(self, db: AdmissionRecords, run_id: str, kind: str, claim: str) -> bool:
        if any(dep.status != "accepted" for dep in db.dependencies(run_id)):
            return False
        active = db.active_claims()
        if not self.slots.free(kind, (running for running, _ in active)):
            return False
        workspace = self.runs.workspace
        for other, held in db.unfinished_claims(run_id):
            if not workspace.overlaps(claim, held):
                continue
            # Workflows are prefetched outside the transaction; unknown ones hold.
            flow = self.runs.cached_flow(other.workflow_digest)
            if flow is None or machine.holds_claim(other, flow):
                return False
        return not any(workspace.overlaps(claim, other) for _, other in active)

    def admissible(self, run_id: str) -> bool:
        """Could the run take a slot and its paths now? A cheap check the coordinator
        makes before any expensive observation (lanes, Git revisions)."""
        store = self.runs.store
        with store.unit() as db:
            run = db.run(run_id)
            claim = db.location(run_id)[1]
            others = db.unfinished_claims(run_id)
        for other, _ in others:
            self.runs.flow(other.workflow_digest)
        kind = self.runs.flow(run.workflow_digest).step(run.step).kind
        if kind not in PROCESS_KINDS:  # no process: no slot and no paths
            return True
        with store.unit() as db:
            return self._admitted(db, run_id, kind, claim)

    def dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        return optimistic(
            lambda: self._dispatch(run_id, now, attempt_id), "Task kept changing during dispatch"
        )

    def _dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        runs = self.runs
        with runs.store.unit() as db:
            run = db.run(run_id)
            root, claim = db.location(run_id)
            results = db.results(run_id)
            others = db.unfinished_claims(run_id)
        if not machine.dispatchable(run, now):
            # Paused, answered, blocked or sleeping since the caller looked: skip, never block.
            raise Conflict("Task is not dispatchable now")
        for other, _ in others:
            runs.flow(other.workflow_digest)
        workflow = runs.flow(run.workflow_digest)
        step = workflow.step(run.step)
        kind = step.kind
        facts = data_of(results, run.previous_attempt) if kind == "condition" else "{}"
        transition = machine.dispatch(run, workflow, now, attempt_id, facts)
        if kind == "finish" and run.gates:
            if runs.revision_of(claim) != run.revision:
                raise ValueError("Acceptance revision changed")
            for result in results:
                if result.revision == run.revision and result.outcome == "passed":
                    runs.workspace.verify(result, root, run.revision)
        with runs.store.unit() as db:
            if kind == "agent" and transition.effects:
                if self.budget.exhausted(*db.queue_usage(), step.options.planning):
                    held = machine.limit(run, now, "queue_limit", machine.QUEUE_LIMIT)
                    return db.apply(run, held)
            if kind != "condition" and not self._admitted(db, run_id, kind, claim):
                raise Conflict("Waiting for dependency or resource ownership")
            # CAS against the snapshot: the observations above belong to this version.
            return db.apply(run, transition)
