"""A separately installed sample step/provider using only the public SDK."""

import hashlib
import sys
from pathlib import Path

from sdd_core.execution import ExecutionHandle, ExecutionObservation, ExecutionRequest
from sdd_core.models import Artifact, Result, Usage
from sdd_core.sdk import Launch, Manifest, Packet


class ExampleHandler:
    manifest = Manifest("example", "0.1.0", capabilities=("agent", "check", "operation"))

    def prepare(self, packet: Packet) -> Launch:
        return Launch(
            (sys.executable, "-c", "print('external plugin completed')"), packet.workspace
        )

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        path = Path(packet.directory) / "stdout.log"
        proof = Artifact(
            path.relative_to(Path(packet.workspace)).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            revision,
        )
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            "failed" if exit_code else ("passed" if packet.step.gate else "done"),
            "External deterministic test provider",
            revision,
            (proof,),
            Usage(0, 0, 0),
            True,
            True,
        )


class ExampleProject:
    manifest = Manifest("example-project", "0.1.0", capabilities=("revision",))

    def revision(self, workspace: str) -> str:
        path = Path(workspace) / "project.txt"
        return hashlib.sha256(path.read_bytes()).hexdigest()


class ExampleExecutionBackend:
    """Volatile test executor; losing this object yields unknown, never terminated."""

    id = "example"

    def __init__(self) -> None:
        self.requests: dict[str, ExecutionRequest] = {}
        self.cancelled: set[str] = set()

    def start(self, request: ExecutionRequest) -> ExecutionHandle:
        if request.id in self.requests and self.requests[request.id] != request:
            raise ValueError("Conflicting execution request")
        self.requests[request.id] = request
        return ExecutionHandle(self.id, request.id, request.generation)

    def reconcile(self, handle: ExecutionHandle) -> ExecutionObservation:
        request = self.requests.get(handle.id)
        if request is None:
            return ExecutionObservation(handle, "unknown")
        if handle.backend != self.id or handle.generation != request.generation:
            raise ValueError("Foreign execution handle")
        return ExecutionObservation(
            handle, "terminated" if handle.id in self.cancelled else "running"
        )

    def cancel(self, handle: ExecutionHandle) -> None:
        self.reconcile(handle)
        self.cancelled.add(handle.id)
