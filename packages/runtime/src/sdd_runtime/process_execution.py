"""Local command implementation of the same contract used by HTTP executors."""

import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import psutil
from sdd_core.codec import canonical, integer, number, object_json, sequence, text
from sdd_core.execution import ExecutionHandle, ExecutionObservation, ExecutionRequest
from sdd_core.models import Result

from sdd_runtime.files import atomic_write, evidence, revision
from sdd_runtime.lock import Lease
from sdd_runtime.platform import NO_WINDOW, Containment, Job, group_alive


class ProcessExecutionBackend:
    """Trusted command payload: argv, workspace, optional environment and gate.

    Each durable attempt directory belongs to this backend instance's root.
    Uncertain startup is never retried automatically. Callers hold the queue lease.
    This backend does not interpret agent output; providers supply separate handlers.
    """

    def __init__(self, root: Path, identifier: str = "local-command") -> None:
        self.id, self.root = identifier, root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.live: dict[str, tuple[subprocess.Popen[bytes], Containment]] = {}

    def _folder(self, identifier: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
            raise ValueError("Invalid execution id")
        return self.root / identifier

    def start(self, request: ExecutionRequest) -> ExecutionHandle:
        folder = self._folder(request.id)
        folder.mkdir(exist_ok=True)
        handle = ExecutionHandle(self.id, request.id, request.generation)
        with Lease(folder / "launch.lock"):
            record = folder / "request.json"
            document = canonical(asdict(request))
            if record.exists():
                if record.read_text(encoding="utf-8") != document:
                    raise ValueError("Conflicting execution request")
                return handle
            config = object_json(request.payload)
            argv = tuple(text(x, "argv") for x in sequence(config.get("argv")))
            workspace = Path(text(config.get("workspace"), "workspace")).resolve(strict=True)
            if not argv or not Path(argv[0]).is_absolute():
                raise ValueError("Explicit executable required")
            if not folder.is_relative_to(workspace / ".sdd-engine"):
                raise ValueError("Local command receipts must live in workspace engine scratch")
            nonce = uuid.uuid4().hex
            atomic_write(
                folder / "launch.json",
                canonical(
                    {
                        "argv": argv,
                        "cwd": str(workspace),
                        "environment": config.get("environment", {}),
                        "nonce": nonce,
                        "parent_pid": os.getpid(),
                    }
                ),
            )
            # The existence of this record fences repeated starts, even on ambiguous failure.
            atomic_write(record, document)
            job = Job()
            child: subprocess.Popen[bytes] | None = None
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", "sdd_runtime.host", str(folder)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                    start_new_session=os.name != "nt",
                )
                job.assign(child.pid)
                atomic_write(
                    folder / "identity.json",
                    canonical(
                        {
                            "pid": child.pid,
                            "created": psutil.Process(child.pid).create_time(),
                            "nonce": nonce,
                        }
                    ),
                )
                assert child.stdin is not None
                child.stdin.write(b"GO\n")
                child.stdin.close()
                self.live[request.id] = (child, job)
            except BaseException:
                job.close()
                if child:
                    if child.stdin and not child.stdin.closed:
                        child.stdin.close()
                    child.wait(timeout=5)
                raise
        return handle

    def reconcile(self, handle: ExecutionHandle) -> ExecutionObservation:
        if handle.backend != self.id:
            raise ValueError("Foreign backend handle")
        folder = self._folder(handle.id)
        request_path = folder / "request.json"
        identity_path = folder / "identity.json"
        if not request_path.exists() or not identity_path.exists():
            return ExecutionObservation(handle, "unknown", reason="Launch identity unconfirmed")
        request = object_json(request_path.read_text(encoding="utf-8"))
        if request.get("generation") != handle.generation:
            raise ValueError("Stale execution generation")
        identity = object_json(identity_path.read_text(encoding="utf-8"))
        pid = integer(identity["pid"], "pid")
        live = self.live.get(handle.id)
        if live:
            child, job = live
            if child.poll() is None:
                return ExecutionObservation(handle, "running")
            job.stop_and_confirm()
            job.close()
            del self.live[handle.id]
        else:
            try:
                process = psutil.Process(pid)
                if abs(process.create_time() - number(identity["created"])) < 0.01:
                    return ExecutionObservation(
                        handle, "unknown", reason="Live host requires ownership reconciliation"
                    )
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                return ExecutionObservation(handle, "unknown", reason="Cannot inspect host")
            if os.name != "nt" and group_alive(pid):
                return ExecutionObservation(handle, "unknown", reason="Process group still exists")
        config = object_json(text(request["payload"], "payload"))
        workspace = Path(text(config["workspace"], "workspace"))
        exit_path = folder / "exit.json"
        if not exit_path.exists():
            return ExecutionObservation(handle, "terminated", reason="No durable completion")
        receipt = object_json(exit_path.read_text(encoding="utf-8"))
        if receipt.get("nonce") != identity["nonce"]:
            raise ValueError("Foreign host receipt")
        current = revision(workspace)
        succeeded = integer(receipt["exit_code"], "exit_code") == 0
        outcome = ("passed" if config.get("gate") is True else "done") if succeeded else "failed"
        result = Result(
            handle.id,
            handle.generation,
            outcome,
            "Command completed",
            current,
            (evidence(folder / "stdout.log", workspace, current),),
        )
        return ExecutionObservation(
            handle, "completed", result, completed_at=number(receipt["completed_at"])
        )

    def cancel(self, handle: ExecutionHandle) -> None:
        if handle.backend != self.id:
            raise ValueError("Foreign backend handle")
        request_path = self._folder(handle.id) / "request.json"
        if not request_path.exists():
            return
        if (
            object_json(request_path.read_text(encoding="utf-8")).get("generation")
            != handle.generation
        ):
            raise ValueError("Stale execution generation")
        if handle.id in self.live:
            child, job = self.live[handle.id]
            job.stop_and_confirm()
            child.wait(timeout=5)
        # A lost host handle is not authority to kill an arbitrary reused PID.

    def close(self) -> None:
        for child, job in self.live.values():
            job.stop_and_confirm()
            child.wait(timeout=5)
            job.close()
        self.live.clear()
