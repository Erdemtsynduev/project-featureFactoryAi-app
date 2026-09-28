"""Single-writer coordinator with gated hosts and durable per-attempt receipts."""

import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import psutil
from sdd_core.codec import canonical, integer, number, object_json, result_json, result_load
from sdd_core.machine import changed
from sdd_core.models import Result
from sdd_core.ports import Conflict
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import atomic_write, verify_evidence
from sdd_runtime.platform import NO_WINDOW, Containment, Job, group_alive


@dataclass
class Running:
    packet: Packet
    process: subprocess.Popen[bytes]
    job: Containment
    nonce: str


class Coordinator:
    def __init__(
        self, engine: ApplicationEngine, registry: Registry, health_path: Path | None = None
    ) -> None:
        self.engine, self.registry = engine, registry
        self.health_path = health_path
        self.live: dict[str, Running] = {}
        self.resource_job = Job()

    def revision(self, root: Path) -> str:
        return self.engine.project.revision(str(root))

    def block(self, run_id: str, now: float, reason: str) -> None:
        with self.engine.store.unit() as db:
            before = db.run(run_id)
            db.apply(
                before,
                changed(replace(before, status="blocked", reason=reason), now, "blocked", reason),
            )

    def bind(self, run_id: str) -> None:
        workflow = self.engine.store.workflow(self.engine.store.get(run_id).workflow_digest)
        with self.engine.store.unit() as db:
            for step in workflow.steps:
                if step.kind not in ("agent", "check", "operation"):
                    continue
                manifest = self.registry.get(handler_key(step)).manifest
                required_profile = object_json(step.config).get("profile_snapshot")
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

    def _packet(self, run_id: str) -> Packet:
        store = self.engine.store
        run = store.get(run_id)
        if run.active is None:
            raise ValueError("No dispatched attempt")
        workflow = store.workflow(run.workflow_digest)
        with store.unit() as db:
            root = Path(db.location(run_id)[0])
            context = db.context(run_id)
            results = db.recent_results(run_id, 5)
        folder = root / ".sdd-engine" / run.id / run.active.id
        folder.mkdir(parents=True, exist_ok=True)
        if results:
            remaining = workflow.max_input_chars - len(context)
            carry = "\nRecent results (newest first):\n" + "\n".join(results)
            if remaining < len(carry):
                carry = "\nPrevious result artifact: " + str(
                    folder.parent / str(run.previous_attempt) / "receipt.json"
                )
            if len(context) + len(carry) > workflow.max_input_chars:
                raise ValueError("Required context exceeds budget")
            context += carry
        return Packet(run.id, run.active, workflow.step(run.step), str(root), str(folder), context)

    def start(self, run_id: str) -> None:
        with self.engine.store.unit() as db:
            active = db.run(run_id).active
            if active and db.execution(active.id) is not None:
                raise Conflict("Attempt belongs to a neutral execution backend")
        self.bind(run_id)
        packet = self._packet(run_id)
        folder = Path(packet.directory)
        nonce = uuid.uuid4().hex
        handler = self.registry.get(handler_key(packet.step))
        launch = handler.prepare(packet)
        if Path(launch.cwd).resolve() != Path(packet.workspace).resolve():
            raise ValueError("Handler cwd must match the owned workspace")
        if not launch.argv or not Path(launch.argv[0]).is_absolute():
            raise ValueError("Explicit executable required")
        atomic_write(folder / "packet.json", canonical(asdict(packet)))
        atomic_write(
            folder / "launch.json",
            canonical(
                {
                    "argv": launch.argv,
                    "cwd": launch.cwd,
                    "environment": dict(launch.environment),
                    "nonce": nonce,
                    "parent_pid": os.getpid(),
                }
            ),
        )
        # Until the persisted host identity exists, no GO can be sent.
        with self.engine.store.unit() as db:
            db.claim_host(packet.attempt.id, canonical(asdict(packet)), nonce)
        job = Job()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "sdd_runtime.host", str(folder)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
                start_new_session=os.name != "nt",
            )
            self.resource_job.assign(process.pid)
            job.assign(process.pid)
            birth = psutil.Process(process.pid).create_time()
            with self.engine.store.unit() as db:
                db.host_started(packet.attempt.id, nonce, process.pid, birth)
            if process.stdin is None:
                raise RuntimeError("Missing host gate")
            process.stdin.write(b"GO\n")
            process.stdin.close()
            self.live[packet.attempt.id] = Running(packet, process, job, nonce)
        except BaseException:
            job.close()
            if process:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                self.resource_job.forget(process.pid)
            raise

    def collect(self, identifier: str, now: float) -> None:
        live = self.live[identifier]
        packet = live.packet
        code = live.process.poll()
        if code is None and now < packet.attempt.deadline:
            return
        if code is None:
            live.job.stop_and_confirm()
            live.process.wait(timeout=10)
            live.job.close()
            self.resource_job.forget(live.process.pid)
            del self.live[identifier]
            self.engine.recover(
                packet.run_id, now, True, "Attempt timeout", self.revision(Path(packet.workspace))
            )
            return
        live.job.stop_and_confirm()
        live.job.close()
        self.resource_job.forget(live.process.pid)
        del self.live[identifier]
        current = self.revision(Path(packet.workspace))
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
            if row.kind in ("human", "condition"):
                continue
            confirmed = row.pid is None
            if row.pid is not None and row.created is not None:
                try:
                    process = psutil.Process(int(row.pid))
                    confirmed = abs(process.create_time() - row.created) > 0.01
                    if not confirmed:
                        try:
                            process.wait(timeout=2)
                            confirmed = True
                        except psutil.TimeoutExpired:
                            pass
                except psutil.NoSuchProcess:
                    confirmed = True
                except psutil.AccessDenied:
                    confirmed = False
                if os.name != "nt" and group_alive(int(row.pid)):
                    confirmed = False
            root = Path(str(row.workspace))
            receipt = root / ".sdd-engine" / run_id / run.active.id / "receipt.json"
            current = self.revision(root)
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
                        packet = self._packet(run_id)
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

    def answer(self, run_id: str, outcome: str, answer: str, now: float) -> None:
        run = self.engine.store.get(run_id)
        workflow = self.engine.store.workflow(run.workflow_digest)
        if run.active is None or workflow.step(run.step).kind != "human":
            raise ValueError("Not waiting for a human")
        # Human waits do not expire like a subprocess; record response against the held attempt.
        self.engine.complete(
            run_id,
            Result(
                run.active.id,
                run.active.generation,
                outcome,
                answer,
                run.revision,
                data=canonical({"answer": answer}),
            ),
            now,
        )

    def tick(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        for identifier in tuple(self.live):
            running = self.live[identifier]
            state = self.engine.store.get(running.packet.run_id)
            if state.paused and state.reason == "Stop requested":
                running.job.stop_and_confirm()
                running.process.wait(timeout=10)
                running.job.close()
                self.resource_job.forget(running.process.pid)
                del self.live[identifier]
                self.engine.recover(
                    state.id,
                    current_time,
                    True,
                    "Stopped by operator",
                    self.revision(Path(running.packet.workspace)),
                )
                continue
            try:
                self.collect(identifier, current_time)
            except (ValueError, OSError) as error:
                run = self.engine.store.get(running.packet.run_id)
                if run.active:
                    self.engine.recover(
                        run.id, current_time, identifier not in self.live, str(error), run.revision
                    )
        with self.engine.store.unit() as db:
            ids = db.runnable()
        dispatched = 0
        for run_id in ids:
            run = self.engine.store.get(run_id)
            if (
                run.paused
                or run.active
                or run.status in ("blocked", "accepted")
                or (run.wake_at and run.wake_at > current_time)
            ):
                continue
            with self.engine.store.unit() as db:
                root = Path(db.location(run_id)[0])
            observed = self.revision(root)
            if observed != run.revision:
                self.engine.invalidate(run_id, observed, current_time)
                continue
            workflow = self.engine.store.workflow(run.workflow_digest)
            try:
                self.bind(run_id)
            except (ValueError, KeyError) as error:
                self.block(run_id, current_time, str(error))
                continue
            try:
                run = self.engine.dispatch(run_id, current_time, uuid.uuid4().hex)
            except Conflict:
                continue
            except ValueError as error:
                self.block(run_id, current_time, str(error))
                continue
            if run.active is None:
                continue
            dispatched += 1
            step = workflow.step(run.step)
            if step.kind == "human":
                continue
            if step.kind == "condition":
                packet = self._packet(run_id)
                config = object_json(packet.step.config)
                outcome = (
                    "true" if config.get(step.condition_key) == step.condition_value else "false"
                )
                self.engine.complete(
                    run_id,
                    Result(
                        run.active.id,
                        run.active.generation,
                        outcome,
                        "Deterministic condition",
                        run.revision,
                    ),
                    current_time,
                )
                continue
            try:
                self.start(run_id)
            except (ValueError, KeyError, OSError) as error:
                self.engine.recover(
                    run_id, current_time, True, "Preflight: " + str(error), self.revision(root)
                )
        with self.engine.store.unit() as db:
            event = db.last_transition()
        if self.health_path is not None:
            atomic_write(
                self.health_path,
                canonical(
                    {
                        "coordinator_pid": os.getpid(),
                        "heartbeat": current_time,
                        "active_attempts": sorted(self.live),
                        "last_transition": event,
                        "resource_limits": (
                            {"cpu_percent": 50, "memory_mb": 16384, "processes": 128}
                            if os.name == "nt"
                            else None
                        ),
                        "containment": "windows-job"
                        if os.name == "nt"
                        else "cooperative-posix-group",
                    }
                ),
            )
        return dispatched

    def close(self) -> None:
        for live in self.live.values():
            live.job.close()
            live.process.wait(timeout=10)
            self.resource_job.forget(live.process.pid)
        self.live.clear()
        self.resource_job.close()
