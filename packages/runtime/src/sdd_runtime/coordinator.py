"""Single-writer scheduler: dispatches runs, submits their launches, applies what ends.

The coordinator never touches a process. Every attempt goes through one path:
`Engine.dispatch` persists it, the step's handler prepares a launch plan,
`ExecutionDriver.submit` records the request before the `Supervisor` starts it,
and each tick `ExecutionDriver.poll` applies the supervisor's observation. The
supervisor reports an end only once the attempt's sandbox is confirmed empty.
"""

import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from sdd_core import machine
from sdd_core.codec import canonical, integer, number, object_json, result_json, result_load, text
from sdd_core.execution import ExecutionRequest
from sdd_core.models import Result
from sdd_core.ports import Conflict
from sdd_core.records import EffectRecord
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.execution import ExecutionDriver
from sdd_runtime.files import atomic_write, verify_evidence
from sdd_runtime.lane_keeper import LaneKeeper
from sdd_runtime.packets import build_packet
from sdd_runtime.supervisor import INPUT_FILE, Plan, Supervisor, health, host_alive

__all__ = ["Coordinator"]

# Windows refuses a command line past 32,767 characters; keep a margin for quoting.
COMMAND_LINE_CHARS = 32000
# How often `contain` re-reads a run that moved while its failure was being recorded.
CONTAIN_RETRIES = 3
RECEIPT_FILE = "receipt.json"


class Coordinator:
    """Schedules runs and applies their executions. One failing run never stops the others.

    A per-run failure is recorded durably on that run (blocked, or recovered when
    it holds an attempt). Only a failure to record it propagates to the caller.
    """

    def __init__(
        self, engine: ApplicationEngine, registry: Registry, health_path: Path | None = None
    ) -> None:
        self.engine, self.registry = engine, registry
        self.health_path = health_path
        self.supervisor = Supervisor(self._request, self._collect)
        self.driver = ExecutionDriver(engine, self.supervisor)
        self.lanes = LaneKeeper(engine)
        # Packets of attempts this process submitted; a restart rebuilds them.
        self.packets: dict[str, Packet] = {}

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
                    if run.status not in ("blocked", "accepted"):
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

    def bind(self, run_id: str) -> None:
        workflow = self.engine.store.workflow(self.engine.store.get(run_id).workflow_digest)
        with self.engine.store.unit() as db:
            for step in workflow.steps:
                if step.kind not in ("agent", "check", "operation"):
                    continue
                manifest = self.registry.get(handler_key(step)).manifest
                required_profile = step.options.profile_snapshot
                if not step.handler and step.profile != "default" and required_profile is None:
                    raise ValueError("Resolve named profiles when creating the run with --config")
                if (
                    required_profile is not None
                    and object_json(manifest.settings).get("profile") != required_profile
                ):
                    raise ValueError("Run profile differs from its creation snapshot")
                if step.kind not in manifest.capabilities:
                    raise ValueError(f"Handler {manifest.id} lacks {step.kind} capability")
                document = canonical(asdict(manifest))
                db.bind_handler(run_id, handler_key(step), document)

    def packet(self, run_id: str) -> Packet:
        return build_packet(self.engine.store, self.registry, run_id)

    # Submission ----------------------------------------------------------------

    def submit(self, run_id: str) -> None:
        """Prepare the dispatched attempt's launch and hand it to the supervisor."""
        self.bind(run_id)
        packet = self.packet(run_id)
        folder = Path(packet.directory)
        launch = self.registry.get(handler_key(packet.step)).prepare(packet)
        if not Path(launch.cwd).resolve().is_relative_to(Path(packet.workspace).resolve()):
            raise ValueError("Handler cwd must stay inside the owned workspace")
        if not launch.argv or not Path(launch.argv[0]).is_absolute():
            raise ValueError("Explicit executable required")
        if len(subprocess.list2cmdline(launch.argv)) > COMMAND_LINE_CHARS:
            raise ValueError("Command line too long; the handler must pass its prompt on stdin")
        atomic_write(folder / "packet.json", canonical(asdict(packet)))
        if launch.input:
            atomic_write(folder / INPUT_FILE, launch.input)
        plan = Plan(
            str(folder),
            packet.workspace,
            launch.argv,
            launch.cwd,
            tuple(sorted(launch.environment)),
            INPUT_FILE if launch.input else "",
        )
        self.packets[packet.attempt.id] = packet
        self.driver.submit(run_id, plan.payload())

    def _request(self, attempt: str) -> ExecutionRequest | None:
        """The durable request the store holds for one of this supervisor's attempts."""
        with self.engine.store.unit() as db:
            row = db.execution(attempt)
        if row is None or row[0] != self.supervisor.id:
            return None
        doc = object_json(row[1])
        return ExecutionRequest(
            text(doc["id"], "id"),
            integer(doc["generation"], "generation"),
            text(doc["handler"], "handler"),
            text(doc["payload"], "payload"),
            number(doc["deadline"]),
        )

    def _collect(self, request: ExecutionRequest, plan: Plan, exit_code: int) -> Result:
        """The handler's result for a quiescent attempt, bound to the owned revision.

        The receipt is written before the result is applied, so a restart reapplies
        the same result instead of asking the handler (or the model) again.
        """
        packet = self.packets.pop(request.id, None)
        if packet is None:
            with self.engine.store.unit() as db:
                run_id = db.effect(request.id).run_id
            self.bind(run_id)
            packet = self.packet(run_id)
        current = self.revision(packet.run_id)
        receipt = Path(plan.folder) / RECEIPT_FILE
        if receipt.exists():
            result = result_load(receipt.read_text(encoding="utf-8"))
            if result.revision != current:
                raise ValueError("Workspace changed after completion")
        else:
            result = self._handler_result(packet, exit_code, current)
        verify_evidence(result.artifacts, Path(packet.workspace), current)
        atomic_write(receipt, result_json(result))
        return result

    def _handler_result(self, packet: Packet, exit_code: int, current: str) -> Result:
        try:
            return self.registry.get(handler_key(packet.step)).collect(packet, exit_code, current)
        except ValueError as error:
            # The attempt is quiescent: bad protocol is a product-independent blocker,
            # not a reason to invoke the model again and lose the primary diagnosis.
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                "Provider protocol: " + str(error),
                current,
            )

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
            rows = db.effects(("pending", "running", "uncertain"))
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
        self.engine.recover(row.run_id, now, confirmed, reason, self.revision(row.run_id))

    # Scheduling ------------------------------------------------------------------

    def _advance(self, run_id: str, now: float) -> bool:
        """Dispatch one runnable run and submit its attempt. True when an attempt began."""
        run = self.engine.store.get(run_id)
        if not machine.dispatchable(run, now):
            return False
        # Cheap checks first: dependencies, process slots and claimed paths. Only a run
        # that could start now pays for lanes and Git revisions.
        if not self.engine.admissible(run_id):
            return False
        workflow = self.engine.store.workflow(run.workflow_digest)
        if any(step.handler.startswith("lane-") for step in workflow.steps):
            try:
                self.lanes.open(run_id, now)
            except (ValueError, RuntimeError, OSError, Conflict) as error:
                self.block(run_id, now, "Lane: " + str(error))
                return False
            run = self.engine.store.get(run_id)
        observed = self.revision(run_id)
        if observed != run.revision:
            self.engine.invalidate(run_id, observed, now)
            return False
        try:
            self.bind(run_id)
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
            self.engine.recover(
                run_id, now, True, "Preflight: " + str(error), self.revision(run_id)
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
