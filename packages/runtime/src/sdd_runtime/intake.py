"""Results and lost attempts: how the end of an attempt reaches its run, once."""

from sdd_core import machine
from sdd_core.codec import digest, result_json
from sdd_core.models import Result, Run
from sdd_core.ports import Conflict, StaleVersion

from sdd_runtime.runs import RunContext, optimistic, received


class ResultIntake:
    def __init__(self, runs: RunContext) -> None:
        self.runs = runs

    def _normalize(self, run_id: str, result: Result) -> Result:
        if not result.artifacts:
            return result
        with self.runs.store.unit() as db:
            workspace = db.location(run_id)[0]
        return self.runs.workspace.normalize(run_id, result, workspace)

    def complete(self, run_id: str, result: Result, now: float, expected: int | None = None) -> Run:
        """Apply a result once. `expected` pins the run version an operator answered."""
        result = self._normalize(run_id, result)
        document = result_json(result)
        fingerprint = digest(document)
        receipt = (fingerprint, run_id)
        runs = self.runs

        def attempt() -> Run:
            with runs.store.unit() as db:
                if received(db, result.attempt_id, receipt):
                    return db.run(run_id)
                run = db.run(run_id)
                root, claim = db.location(run_id)
            if expected is not None and run.version != expected:
                raise Conflict("Stale answer; refresh the task")
            if result.artifacts:
                runs.workspace.verify(result, root, result.revision)
                if runs.revision_of(claim) != result.revision:
                    raise ValueError("Result no longer matches workspace")
            transition = machine.complete(run, runs.flow(run.workflow_digest), result, now)
            try:
                with runs.store.unit() as db:
                    if received(db, result.attempt_id, receipt):
                        return db.run(run_id)
                    state = db.apply(run, transition)
                    db.save_result(result.attempt_id, fingerprint, document)
                    return state
            except StaleVersion:
                if expected is not None:
                    raise Conflict("Stale answer; refresh the task") from None
                raise

        return optimistic(attempt, "Task kept changing while its result was applied")

    def recover(self, run_id: str, now: float, confirmed: bool, reason: str, revision: str) -> Run:
        workflow = self.runs.workflow_of(run_id)
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            transition = machine.recover(
                run,
                now,
                termination_confirmed=confirmed,
                reason=reason,
                observed_revision=revision,
                recovery_step=machine.recovery_route(run, workflow, confirmed, revision),
            )
            state = db.apply(run, transition)
            if run.active:
                db.effect_status(run.active.id, "abandoned" if confirmed else "uncertain")
            return state

    def release_condition(self, run_id: str, now: float) -> Run:
        """Return a condition attempt persisted by an earlier release to pure routing."""
        workflow = self.runs.workflow_of(run_id)
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            if run.active is None or workflow.step(run.active.step).kind != "condition":
                raise ValueError("No persisted condition attempt")
            state = db.apply(run, machine.release_condition(run, now))
            db.effect_status(run.active.id, "abandoned")
            return state
