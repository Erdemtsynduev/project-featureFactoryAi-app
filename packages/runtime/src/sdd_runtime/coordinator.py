"""Single-writer coordinator with gated hosts and durable per-attempt receipts."""

import os
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from sdd_core.codec import canonical, integer, number, object_json, result_json, result_load
from sdd_core.models import Result
from sdd_core.options import StepOptions
from sdd_core.ports import Conflict
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import atomic_write, attempt_folder, verify_evidence
from sdd_runtime.hosts import Hosts, Running, health
from sdd_runtime.lane_keeper import LaneKeeper
from sdd_runtime.packets import build_packet

__all__ = ["Coordinator", "Running"]

# Windows refuses a command line past 32,767 characters; keep a margin for quoting.
COMMAND_LINE_CHARS = 32000
# The attempt file the host feeds to the process's stdin.
INPUT_FILE = "input.txt"


class Coordinator:
    """Schedules runs and owns their hosts. One failing run never stops the others.

    A per-run failure is recorded durably on that run (blocked, or recovered when
    it holds an attempt). Only a failure to record it propagates to the caller.
    """

    def __init__(
        self, engine: ApplicationEngine, registry: Registry, health_path: Path | None = None
    ) -> None:
        self.engine, self.registry = engine, registry
        self.health_path = health_path
        self.live: dict[str, Running] = {}
        self.hosts = Hosts()
        self.lanes = LaneKeeper(engine)

    def revision(self, run_id: str) -> str:
        return self.engine.observe(run_id)

    def block(self, run_id: str, now: float, reason: str) -> None:
        self.engine.block(run_id, now, reason)

    def contain(self, run_id: str, now: float, error: Exception) -> None:
        """Record a failure on its own run so the queue can go on."""
        reason = f"{type(error).__name__}: {error}"
        for _ in range(3):
            run = self.engine.store.get(run_id)
            try:
                if run.active is None:
                    if run.status != "blocked":
                        self.engine.block(run_id, now, reason)
                else:
                    # A host still tracked here may be alive: ownership stays uncertain.
                    confirmed = run.active.id not in self.live
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
                required_profile = StepOptions.parse(step.config).profile_snapshot
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

    def start(self, run_id: str) -> None:
        with self.engine.store.unit() as db:
            active = db.run(run_id).active
            if active and db.execution(active.id) is not None:
                raise Conflict("Attempt belongs to a neutral execution backend")
        self.bind(run_id)
        packet = self.packet(run_id)
        folder = Path(packet.directory)
        nonce = uuid.uuid4().hex
        launch = self.registry.get(handler_key(packet.step)).prepare(packet)
        if not Path(launch.cwd).resolve().is_relative_to(Path(packet.workspace).resolve()):
            raise ValueError("Handler cwd must stay inside the owned workspace")
        if not launch.argv or not Path(launch.argv[0]).is_absolute():
            raise ValueError("Explicit executable required")
        if len(subprocess.list2cmdline(launch.argv)) > COMMAND_LINE_CHARS:
            raise ValueError("Command line too long; the handler must pass its prompt on stdin")
        atomic_write(folder / "packet.json", canonical(asdict(packet)))
        document: dict[str, object] = {
            "argv": launch.argv,
            "cwd": launch.cwd,
            "environment": dict(launch.environment),
            "nonce": nonce,
            "parent_pid": os.getpid(),
        }
        if launch.input:
            atomic_write(folder / INPUT_FILE, launch.input)
            document["input"] = INPUT_FILE
        atomic_write(folder / "launch.json", canonical(document))
        # Until the persisted host identity exists, no GO can be sent.
        with self.engine.store.unit() as db:
            db.claim_host(packet.attempt.id, canonical(asdict(packet)), nonce)

        def started(pid: int, created: float) -> None:
            with self.engine.store.unit() as db:
                db.host_started(packet.attempt.id, nonce, pid, created)

        self.live[packet.attempt.id] = self.hosts.spawn(packet, nonce, started)

    def collect(self, identifier: str, now: float) -> None:
        live = self.live[identifier]
        packet = live.packet
        state = self.engine.store.get(packet.run_id)
        if state.paused and state.reason == "Stop requested":
            self.hosts.settle(live, wait=True)
            del self.live[identifier]
            self.engine.recover(
                packet.run_id, now, True, "Stopped by operator", self.revision(packet.run_id)
            )
            return
        code = live.process.poll()
        if code is None and now < packet.attempt.deadline:
            return
        if code is None:
            self.hosts.settle(live, wait=True)
            del self.live[identifier]
            self.engine.recover(
                packet.run_id, now, True, "Attempt timeout", self.revision(packet.run_id)
            )
            return
        self.hosts.settle(live, wait=False)
        del self.live[identifier]
        current = self.revision(packet.run_id)
        exit_path = Path(packet.directory) / "exit.json"
        if not exit_path.exists():
            self.engine.recover(
                packet.run_id, now, True, "Host exited without durable completion", current
            )
            return
        exit_data = object_json(exit_path.read_text(encoding="utf-8"))
        if exit_data.get("nonce") != live.nonce:
            raise ValueError("Wrong host receipt")
        result = self._collect_result(packet, integer(exit_data["exit_code"], "exit_code"), current)
        verify_evidence(result.artifacts, Path(packet.workspace), current)
        atomic_write(Path(packet.directory) / "receipt.json", result_json(result))
        self.engine.complete(packet.run_id, result, number(exit_data["completed_at"]))

    def _collect_result(self, packet: Packet, exit_code: int, current: str) -> Result:
        try:
            result = self.registry.get(handler_key(packet.step)).collect(packet, exit_code, current)
        except ValueError as error:
            # The host is quiescent: bad protocol is a product-independent blocker,
            # not a reason to invoke the model again and lose the primary diagnosis.
            result = Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                "Provider protocol: " + str(error),
                current,
            )
        return result

    def restore(self, now: float) -> None:
        """Called under the coordinator lease before any new dispatch."""
        with self.engine.store.unit() as db:
            rows = db.effects(("pending", "running", "uncertain"))
        for row in rows:
            if row.external:
                continue
            run_id = row.run_id
            run = self.engine.store.get(run_id)
            if run.active is None:
                raise RuntimeError("Active effect without an attempt")
            if row.kind == "human":
                continue
            if row.kind == "condition":
                # Earlier releases dispatched conditions as effects; route them purely now.
                self.engine.release_condition(run_id, now)
                continue
            confirmed = self.hosts.ended(row.pid, row.created)
            root = Path(str(row.workspace))
            receipt = attempt_folder(root, run_id, run.active.id) / "receipt.json"
            current = self.revision(run_id)
            exit_path = receipt.with_name("exit.json")
            if confirmed and exit_path.exists():
                try:
                    exit_data = object_json(exit_path.read_text(encoding="utf-8"))
                    completed_at = number(exit_data["completed_at"])
                    if (
                        exit_data.get("nonce") != row.host_nonce
                        or completed_at > run.active.deadline
                    ):
                        raise ValueError("Host receipt is stale")
                    if receipt.exists():
                        result = result_load(receipt.read_text(encoding="utf-8"))
                    else:
                        self.bind(run_id)
                        packet = self.packet(run_id)
                        result = self._collect_result(
                            packet, integer(exit_data["exit_code"], "exit_code"), current
                        )
                        atomic_write(receipt, result_json(result))
                    if result.revision != current:
                        raise ValueError("Workspace changed after completion")
                    verify_evidence(result.artifacts, root, current)
                    self.engine.complete(run_id, result, completed_at)
                    continue
                except (ValueError, OSError, KeyError):
                    pass
            self.engine.recover(
                run_id, now, confirmed, "Coordinator restart reconciliation", current
            )

    def _advance(self, run_id: str, now: float) -> bool:
        """Dispatch one runnable run and start its host. True when an attempt began."""
        run = self.engine.store.get(run_id)
        if (
            run.paused
            or run.active
            or run.status in ("blocked", "accepted")
            or (run.wake_at and run.wake_at > now)
        ):
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
            self.start(run_id)
        except (ValueError, KeyError, OSError) as error:
            self.engine.recover(
                run_id, now, True, "Preflight: " + str(error), self.revision(run_id)
            )
        return True

    def tick(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        for identifier in tuple(self.live):
            run_id = self.live[identifier].packet.run_id
            self._isolated(run_id, current_time, partial(self.collect, identifier, current_time))
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
            atomic_write(self.health_path, canonical(health(tuple(self.live), event, current_time)))
        return dispatched

    def close(self) -> None:
        for live in self.live.values():
            self.hosts.release(live)
        self.live.clear()
        self.hosts.close()
