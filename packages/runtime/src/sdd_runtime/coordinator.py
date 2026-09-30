"""Single-writer scheduler: dispatches runs, submits their launches, applies what ends.

The coordinator never touches a process. Every attempt goes through one path:
`Engine.dispatch` persists it, the step's handler prepares a launch plan,
`ExecutionDriver.submit` records the request before the `Supervisor` starts it,
and each tick `ExecutionDriver.poll` applies the supervisor's observation. The
supervisor reports an end only once the attempt's sandbox is confirmed empty.
"""

import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path

from sdd_core import machine
from sdd_core import revision as revisions
from sdd_core.codec import (
    canonical,
)
from sdd_core.models import LIVE_EFFECT_STATUSES, SETTLED_STATUSES, Step
from sdd_core.ports import Conflict
from sdd_core.records import EffectRecord
from sdd_core.sdk import Manifest, Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.execution import ExecutionDriver
from sdd_runtime.files import atomic_write
from sdd_runtime.lane_keeper import LaneKeeper
from sdd_runtime.launcher import Launcher
from sdd_runtime.receipts import Receipts
from sdd_runtime.supervisor import Supervisor, health, host_alive

__all__ = ["Coordinator"]

# How often `contain` re-reads a run that moved while its failure was being recorded.
CONTAIN_RETRIES = 3


class Coordinator:
    """Schedules runs and applies their executions. One failing run never stops the others.

    A per-run failure is recorded durably on that run (blocked, or recovered when
    it holds an attempt). Only a failure to record it propagates to the caller.
    """

    def __init__(
        self,
        engine: ApplicationEngine,
        registry: Registry,
        health_path: Path | None = None,
        available: Callable[[str], bool] | None = None,
    ) -> None:
        """`available(profile)` says whether an agent profile (or its rotation) can take
        work now; a resting one's steps wait unstarted instead of spending an attempt."""
        self.engine, self.registry = engine, registry
        self.health_path = health_path
        self.available = available
        self.launcher = Launcher(engine, registry)
        self.receipts = Receipts(engine, registry, self.launcher, lambda: self.supervisor.id)
        self.supervisor = Supervisor(self.receipts.request, self.receipts.collect)
        self.driver = ExecutionDriver(engine, self.supervisor)
        self.lanes = LaneKeeper(engine)

    def active(self) -> tuple[str, ...]:
        """Attempts whose processes this coordinator started and still owns."""
        return self.supervisor.active()

    def revision(self, run_id: str) -> str:
        return self.engine.observe(run_id)

    def block(self, run_id: str, now: float, reason: str) -> None:
        self.engine.block(run_id, now, reason)

    def contain(self, run_id: str, now: float, error: Exception) -> None:
        """Record a failure on its own run so the queue can go on."""
        reason = f"{type(error).__name__}: {error}"
        for _ in range(CONTAIN_RETRIES):
            run = self.engine.store.get(run_id)
            try:
                if run.active is None:
                    # Accepted is final; a blocked run already shows its own reason.
                    if run.status not in SETTLED_STATUSES:
                        self.engine.block(run_id, now, reason)
                else:
                    # A host still owned here may be alive: ownership stays uncertain.
                    confirmed = run.active.id not in self.supervisor.active()
                    self.engine.recover(run_id, now, confirmed, reason, run.revision)
                return
            except Conflict:
                continue  # the run moved meanwhile; re-read and record again

    def _isolated(self, run_id: str, now: float, action: Callable[[], object]) -> object:
        try:
            return action()
        except Exception as error:
            self.contain(run_id, now, error)
            return None

    def _auto_answer(self, run_id: str, now: float) -> None:
        try:
            self.engine.auto_answer(run_id, now)
        except (ValueError, Conflict) as error:
            self.block(run_id, now, "Automatic answer: " + str(error))

    def manifest(self, step: Step) -> Manifest:
        return self.launcher.manifest(step)

    def bind(self, run_id: str) -> None:
        self.launcher.bind(run_id)

    def packet(self, run_id: str) -> Packet:
        return self.launcher.packet(run_id)

    @property
    def packets(self) -> dict[str, Packet]:
        return self.receipts.packets

    # Submission ----------------------------------------------------------------

    def submit(self, run_id: str) -> None:
        """Prepare the dispatched attempt's launch and hand it to the supervisor."""
        packet, plan = self.launcher.prepare(run_id)
        self.receipts.packets[packet.attempt.id] = packet
        self.driver.submit(run_id, plan.payload())

    # Observation -----------------------------------------------------------------

    def _executions(self, statuses: tuple[str, ...]) -> tuple[EffectRecord, ...]:
        """Effects submitted to this coordinator's supervisor, in dispatch order."""
        with self.engine.store.unit() as db:
            rows = db.effects(statuses)
            ours = tuple(
                row
                for row in rows
                if row.external and (db.execution(row.id) or ("", ""))[0] == self.supervisor.id
            )
        return ours

    def collect(self, now: float) -> None:
        """Apply every execution that ended; a paused queue still collects."""
        for row in self._executions(("pending", "running")):
            self._isolated(row.run_id, now, partial(self.driver.poll, row.run_id, now))

    def restore(self, now: float) -> None:
        """Called under the coordinator lease before any new dispatch."""
        with self.engine.store.unit() as db:
            rows = db.effects(LIVE_EFFECT_STATUSES)
            requests = {row.id: db.execution(row.id) for row in rows}
        for row in rows:
            if row.kind == "human":
                continue
            run = self.engine.store.get(row.run_id)
            if run.active is None:
                raise RuntimeError("Active effect without an attempt")
            if row.kind == "condition":
                # Earlier releases dispatched conditions as effects; route them purely now.
                self.engine.release_condition(row.run_id, now)
                continue
            request = requests[row.id]
            if request is not None:
                if request[0] == self.supervisor.id:
                    self._isolated(row.run_id, now, partial(self.driver.poll, row.run_id, now))
                continue  # another backend's driver owns it
            self._restore_unsubmitted(row, now)

    def _restore_unsubmitted(self, row: EffectRecord, now: float) -> None:
        """An attempt without an execution request: never submitted, or an older host."""
        if row.pid is None:
            confirmed, reason = True, "Launch never started"
        else:
            # A host launched by a release before the supervisor: prove it is gone.
            gone = host_alive(int(row.pid), float(row.created or 0)) is False
            confirmed, reason = gone, "Coordinator restart reconciliation"
        launched = row.pid is not None
        self.engine.recover(row.run_id, now, confirmed, reason, self.revision(row.run_id), launched)

    # Scheduling ------------------------------------------------------------------

    def _advance(self, run_id: str, now: float) -> bool:
        """Dispatch one runnable run and submit its attempt. True when an attempt began."""
        run = self.engine.store.get(run_id)
        if not machine.dispatchable(run, now):
            return False
        # Cheap checks first: dependencies, process slots and claimed paths. Only a run
        # that could start now pays for lanes and Git revisions.
        workflow = self.engine.store.workflow(run.workflow_digest)
        step = workflow.step(run.step)
        if step.kind == "agent" and self.available and not self.available(handler_key(step)):
            return False  # every profile that could run it rests until its window resets
        if not self.engine.admissible(run_id):
            return False
        if any(step.handler.startswith("lane-") for step in workflow.steps):
            try:
                self.lanes.open(run_id, now)
            except (ValueError, RuntimeError, OSError, Conflict) as error:
                self.block(run_id, now, "Lane: " + str(error))
                return False
            run = self.engine.store.get(run_id)
        observed = self.revision(run_id)
        if observed != run.revision and revisions.earlier_format(run.revision):
            run = self.engine.rebase_revision(run_id, observed, now)
        if observed != run.revision:
            self.engine.invalidate(run_id, observed, now)
            return False
        try:
            self.bind(run_id)
        except Conflict:
            return False  # a handler changed under a live attempt: bind after it ends
        except (ValueError, KeyError) as error:
            self.block(run_id, now, str(error))
            return False
        try:
            run = self.engine.dispatch(run_id, now, uuid.uuid4().hex)
        except Conflict:
            return False  # waiting for a dependency, a process slot or its paths
        except ValueError as error:
            self.block(run_id, now, str(error))
            return False
        if run.active is None:
            return False
        if workflow.step(run.step).kind == "human":
            self.engine.auto_answer(run_id, now)
            return True
        try:
            self.submit(run_id)
        except (ValueError, KeyError, OSError) as error:
            # Preflight failed before any host started: no model could have run.
            self.engine.recover(
                run_id, now, True, "Preflight: " + str(error), self.revision(run_id), False
            )
        return True

    def tick(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        self.collect(current_time)
        with self.engine.store.unit() as db:
            ids = db.runnable()
            states = db.runs()
        answering = [r.id for r in states if r.active is not None and r.status == "running"]
        finished = [
            r.id for r in states if r.status == "accepted" and r.id not in self.lanes.cleaned
        ]
        for run_id in finished:
            self._isolated(run_id, current_time, partial(self.lanes.close, run_id))
        for run_id in answering:
            self._isolated(run_id, current_time, partial(self._auto_answer, run_id, current_time))
        dispatched = 0
        for run_id in ids:
            began = self._isolated(
                run_id, current_time, partial(self._advance, run_id, current_time)
            )
            dispatched += began is True
        if self.health_path is not None:
            with self.engine.store.unit() as db:
                event = db.last_transition()
            atomic_write(self.health_path, canonical(health(self.active(), event, current_time)))
        return dispatched

    def close(self) -> None:
        self.supervisor.close()
