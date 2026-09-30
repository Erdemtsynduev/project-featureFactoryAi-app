"""Durable dispatch for every execution backend: submit once, then poll to an end."""

from sdd_core import machine
from sdd_core.codec import canonical, encode
from sdd_core.execution import (
    ExecutionBackend,
    ExecutionHandle,
    ExecutionRequest,
    validate_observation,
)
from sdd_core.models import Run
from sdd_core.ports import Conflict

from sdd_runtime.application import ApplicationEngine


class ExecutionDriver:
    """The outbox attempt id is the backend's idempotency key.

    After an ambiguous start, call submit again with the same immutable request.
    A backend must look up the existing execution, never start a second copy.
    All calls require the application's single coordinator lease.
    """

    def __init__(self, engine: ApplicationEngine, backend: ExecutionBackend) -> None:
        self.engine, self.backend = engine, backend

    def submit(self, run_id: str, payload: str) -> ExecutionHandle:
        run = self.engine.store.get(run_id)
        if run.active is None:
            raise ValueError("Dispatch must be persisted before execution")
        step = self.engine.store.workflow(run.workflow_digest).step(run.step)
        request = ExecutionRequest(
            run.active.id, run.active.generation, step.handler, payload, run.active.deadline
        )
        document = canonical(encode(request))
        with self.engine.store.unit() as db:
            current = db.run(run_id)
            if current.active != run.active:
                raise Conflict("Active attempt changed before backend binding")
            db.bind_execution(run_id, request.id, self.backend.id, document)
        handle = self.backend.start(request)
        expected = ExecutionHandle(self.backend.id, request.id, request.generation)
        if handle != expected:
            raise ValueError("Backend changed dispatch identity")
        return handle

    def poll(self, run_id: str, now: float) -> None:
        run = self.engine.store.get(run_id)
        if run.active is None:
            return
        with self.engine.store.unit() as db:
            row = db.execution(run.active.id)
        if row is None or row[0] != self.backend.id:
            raise Conflict("No matching durable execution request")
        handle = ExecutionHandle(self.backend.id, run.active.id, run.active.generation)
        observation = self.backend.reconcile(handle)
        validate_observation(handle, observation)
        if observation.status == "running" and (
            now >= run.active.deadline or machine.stop_requested(run)
        ):
            self.backend.cancel(handle)
            # A backend that ends synchronously settles in this same poll.
            observation = self.backend.reconcile(handle)
            validate_observation(handle, observation)
        if observation.status == "completed":
            assert observation.result is not None
            assert observation.completed_at is not None
            if observation.completed_at > now or observation.completed_at < run.active.started:
                raise ValueError("Completion time outside observation interval")
            self.engine.complete(run_id, observation.result, observation.completed_at)
        elif observation.status in ("terminated", "unknown"):
            observed_revision = run.revision
            if observation.status == "terminated":
                observed_revision = self.engine.observe(run_id)
            self.engine.recover(
                run_id,
                now,
                observation.status == "terminated",
                ended_reason(run, now, observation.reason or observation.status),
                observed_revision,
                launched=observation.launched,
            )


def ended_reason(run: Run, now: float, reported: str) -> str:
    """Why an execution ended, from the run itself: the engine's own stop wins."""
    if machine.stop_requested(run):
        return "Stopped by operator"
    if run.active is not None and now >= run.active.deadline:
        return "Attempt timeout"
    return reported
