"""The local execution backend: every process the engine runs is started and ended here.

A request's payload is a launch plan (see `Plan`). The supervisor starts a gated
host in a `Sandbox`, observes its descendants while it runs, and reports an
execution `completed` or `terminated` only once the sandbox is confirmed empty:
the host and everything it started, including descendants that left the OS
container. Anything it cannot prove is `unknown`, which keeps the attempt's
ownership. The durable request is the store's; files live in the attempt folder.

Turning an exit into a `Result` is the caller's (`Collector`): the coordinator
asks the step's handler, a plain command backend reports pass/fail.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
from sdd_core.codec import (
    canonical,
    encode,
    flag,
    integer,
    mapping,
    number,
    object_json,
    sequence,
    text,
)
from sdd_core.execution import ExecutionHandle, ExecutionObservation, ExecutionRequest
from sdd_core.models import Result
from sdd_core.sdk import STDOUT_LOG

from sdd_runtime.files import atomic_write, evidence, revision
from sdd_runtime.interpreters import contained_path
from sdd_runtime.lineage import SAME_PROCESS
from sdd_runtime.lock import Lease
from sdd_runtime.platform import Job, group_alive
from sdd_runtime.sandbox import Sandbox, end_recorded

# Caps of the aggregate Windows job (see windows.py); reported in the health file.
RESOURCE_LIMITS = {"cpu_percent": 50, "memory_mb": 16384, "processes": 128}
REQUEST_FILE = "request.json"
IDENTITY_FILE = "identity.json"
EXIT_FILE = "exit.json"
INPUT_FILE = "input.txt"


@dataclass(frozen=True)
class Plan:
    """What to run for one execution. `folder` holds its files and logs."""

    folder: str
    workspace: str
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...] = ()
    # The file in `folder` the process reads on stdin, written before submission.
    input: str = ""
    # A plain command reports `passed` instead of `done` when it gates acceptance.
    gate: bool = False

    def payload(self) -> str:
        document = asdict(self)
        document["environment"] = dict(self.environment)
        return canonical(document)

    @staticmethod
    def parse(payload: str) -> "Plan":
        doc = object_json(payload)
        argv = tuple(text(x, "argv") for x in sequence(doc["argv"]))
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError("Explicit executable required")
        environment = mapping(doc.get("environment", {}))
        return Plan(
            text(doc["folder"], "folder"),
            text(doc["workspace"], "workspace"),
            argv,
            text(doc["cwd"], "cwd"),
            tuple(sorted((key, text(value, "environment")) for key, value in environment.items())),
            text(doc.get("input", ""), "input"),
            flag(doc.get("gate", False), "gate"),
        )


# (request, plan, exit code) -> the result the attempt reports
Collector = Callable[[ExecutionRequest, Plan, int], Result]


def command_result(request: ExecutionRequest, plan: Plan, exit_code: int) -> Result:
    """A plain command: success is `done` (`passed` for a gate), with stdout as evidence."""
    workspace = Path(plan.workspace)
    current = revision(workspace)
    outcome = ("passed" if plan.gate else "done") if exit_code == 0 else "failed"
    stdout = Path(plan.folder) / STDOUT_LOG
    return Result(
        request.id,
        request.generation,
        outcome,
        "Command completed",
        current,
        (evidence(stdout, workspace, Path(plan.folder), current),),
    )


def read_exit(path: Path, nonce: str) -> tuple[int, float]:
    """The exit code and completion time the host recorded; another nonce is another launch."""
    data = object_json(path.read_text(encoding="utf-8"))
    if data.get("nonce") != nonce:
        raise ValueError("Wrong host receipt")
    return integer(data["exit_code"], "exit_code"), number(data["completed_at"])


def host_alive(pid: int, created: float, grace: float = 2.0) -> bool | None:
    """True while the recorded host runs, False once gone, None when it cannot be told.

    A host whose owner just died is usually ending too: it gets `grace` seconds.
    """
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) >= SAME_PROCESS:
            return False
        try:
            process.wait(timeout=grace)
            return False
        except psutil.TimeoutExpired:
            return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return None


@dataclass
class Execution:
    request: ExecutionRequest
    plan: Plan
    process: subprocess.Popen[bytes]
    sandbox: Sandbox
    nonce: str


class Supervisor:
    """`ExecutionBackend` for local processes. Callers hold the queue's coordinator lease."""

    def __init__(
        self,
        requests: Callable[[str], ExecutionRequest | None] | None = None,
        collect: Collector = command_result,
        identifier: str = "local",
    ) -> None:
        self.id = identifier
        self.lookup, self.collect = requests, collect
        self.aggregate = Job()
        self.live: dict[str, Execution] = {}
        self.known: dict[str, ExecutionRequest] = {}

    # Contract ------------------------------------------------------------------

    def start(self, request: ExecutionRequest) -> ExecutionHandle:
        handle = ExecutionHandle(self.id, request.id, request.generation)
        plan = Plan.parse(request.payload)
        folder = Path(plan.folder)
        if not Path(plan.cwd).resolve().is_relative_to(Path(plan.workspace).resolve()):
            raise ValueError("Launch cwd must stay inside the owned workspace")
        folder.mkdir(parents=True, exist_ok=True)
        document = canonical(encode(request))
        with Lease(folder / "launch.lock"):
            record = folder / REQUEST_FILE
            if record.exists():
                # A repeated start after an ambiguous reply: never a second copy.
                if record.read_text(encoding="utf-8") != document:
                    raise ValueError("Conflicting execution request")
                self.known[request.id] = request
                return handle
            # The record fences repeated starts, even when the launch below fails.
            atomic_write(record, document)
            self.known[request.id] = request
            self.live[request.id] = self._launch(request, plan)
        return handle

    def reconcile(self, handle: ExecutionHandle) -> ExecutionObservation:
        if handle.backend != self.id:
            raise ValueError("Foreign backend handle")
        request = self._request(handle)
        if request is None:
            return ExecutionObservation(handle, "unknown", reason="No execution request")
        if request.generation != handle.generation:
            raise ValueError("Stale execution generation")
        plan = Plan.parse(request.payload)
        live = self.live.get(handle.id)
        if live is not None:
            if live.process.poll() is None:
                live.sandbox.observe()
                return ExecutionObservation(handle, "running")
            # The host exited; what it left running in the background ends with it.
            del self.live[handle.id]
            try:
                live.sandbox.end()
            except (TimeoutError, OSError) as error:
                return ExecutionObservation(handle, "unknown", reason=str(error))
            finally:
                live.sandbox.close()
            return self._finish(handle, request, plan, live.nonce, restored=False)
        return self._restored(handle, request, plan)

    def cancel(self, handle: ExecutionHandle) -> None:
        """End the execution's sandbox; `reconcile` then reports what happened."""
        live = self.live.get(handle.id)
        if live is None:
            return
        try:
            live.sandbox.end()
            live.process.wait(timeout=10)
        except (TimeoutError, OSError, subprocess.TimeoutExpired):
            pass  # a cancel is never proof; reconcile keeps the attempt unknown

    # Queries -------------------------------------------------------------------

    def active(self) -> tuple[str, ...]:
        """Executions whose host this supervisor started and still owns."""
        return tuple(self.live)

    def close(self) -> None:
        """Shutdown: end every owned execution, confirmed or not, and release the jobs."""
        for live in self.live.values():
            try:
                live.sandbox.end()
            except (TimeoutError, OSError):
                pass
            finally:
                live.sandbox.close()
            try:
                live.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.live.clear()
        self.aggregate.close()

    # Internals -----------------------------------------------------------------

    def _request(self, handle: ExecutionHandle) -> ExecutionRequest | None:
        request = self.known.get(handle.id)
        if request is None and self.lookup is not None:
            request = self.lookup(handle.id)
            if request is not None:
                self.known[handle.id] = request
        return request

    def _launch(self, request: ExecutionRequest, plan: Plan) -> Execution:
        folder = Path(plan.folder)
        nonce = uuid.uuid4().hex
        environment = dict(plan.environment)
        # Interpreters behind packaged-app aliases would run outside every job.
        environment.setdefault("PATH", contained_path(os.environ.get("PATH", "")))
        launch: dict[str, object] = {
            "argv": list(plan.argv),
            "cwd": plan.cwd,
            "environment": environment,
            "nonce": nonce,
            "parent_pid": os.getpid(),
        }
        if plan.input:
            if not (folder / plan.input).is_file():
                raise ValueError("Launch input file is missing")
            launch["input"] = plan.input
        atomic_write(folder / "launch.json", canonical(launch))
        sandbox = Sandbox(folder, self.aggregate)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = sandbox.start(
                [sys.executable, "-m", "sdd_runtime.host", str(folder)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Until the persisted host identity exists, no GO can be sent.
            atomic_write(
                folder / IDENTITY_FILE,
                canonical(
                    {
                        "pid": process.pid,
                        "created": psutil.Process(process.pid).create_time(),
                        "nonce": nonce,
                    }
                ),
            )
            if process.stdin is None:
                raise RuntimeError("Missing host gate")
            process.stdin.write(b"GO\n")
            process.stdin.close()
        except BaseException:
            try:
                sandbox.end()
            finally:
                sandbox.close()
                if process is not None:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    process.wait(timeout=5)
            raise
        return Execution(request, plan, process, sandbox, nonce)

    def _restored(
        self, handle: ExecutionHandle, request: ExecutionRequest, plan: Plan
    ) -> ExecutionObservation:
        """An execution this process did not start or no longer tracks: prove its end."""
        folder = Path(plan.folder)
        identity_path = folder / IDENTITY_FILE
        if not identity_path.exists():
            # No GO was ever sent: the host ran nothing and ends with its launcher.
            return ExecutionObservation(
                handle, "terminated", reason="Launch never started", launched=False
            )
        identity = object_json(identity_path.read_text(encoding="utf-8"))
        pid, created = integer(identity["pid"], "pid"), number(identity["created"])
        alive = host_alive(pid, created)
        if alive is not False or (os.name != "nt" and group_alive(pid)):
            return ExecutionObservation(
                handle, "unknown", reason="Live host requires ownership reconciliation"
            )
        try:
            end_recorded(folder)
        except (TimeoutError, OSError) as error:
            return ExecutionObservation(handle, "unknown", reason=str(error))
        return self._finish(handle, request, plan, text(identity["nonce"], "nonce"), restored=True)

    def _finish(
        self,
        handle: ExecutionHandle,
        request: ExecutionRequest,
        plan: Plan,
        nonce: str,
        *,
        restored: bool,
    ) -> ExecutionObservation:
        folder = Path(plan.folder)
        exit_path = folder / EXIT_FILE
        if not exit_path.exists():
            # The host opens the payload's log only after GO, right before it starts.
            launched = (folder / IDENTITY_FILE).exists() and (folder / STDOUT_LOG).exists()
            return ExecutionObservation(
                handle,
                "terminated",
                reason="Host exited without durable completion"
                if launched
                else "Host exited before the payload started",
                launched=launched,
            )
        try:
            exit_code, completed_at = read_exit(exit_path, nonce)
            if restored and completed_at > request.deadline:
                raise ValueError("Host receipt is stale")
            result = self.collect(request, plan, exit_code)
        except (ValueError, OSError, KeyError) as error:
            return ExecutionObservation(handle, "terminated", reason=str(error))
        return ExecutionObservation(handle, "completed", result, completed_at=completed_at)


def health(live: tuple[str, ...], event: float | None, now: float) -> dict[str, object]:
    return {
        "coordinator_pid": os.getpid(),
        "heartbeat": now,
        "active_attempts": sorted(live),
        "last_transition": event,
        "resource_limits": RESOURCE_LIMITS if os.name == "nt" else None,
        "containment": "windows-job" if os.name == "nt" else "cooperative-posix-group",
    }
