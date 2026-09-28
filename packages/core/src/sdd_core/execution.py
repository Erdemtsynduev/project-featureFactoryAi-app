"""Transport-neutral execution contracts. Handles are opaque to the control plane."""

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from sdd_core.models import Result

type ExecutionStatus = Literal["running", "completed", "terminated", "unknown"]


@dataclass(frozen=True)
class ExecutionRequest:
    id: str
    generation: int
    handler: str
    payload: str
    deadline: float


@dataclass(frozen=True)
class ExecutionHandle:
    backend: str
    id: str
    generation: int


@dataclass(frozen=True)
class ExecutionObservation:
    handle: ExecutionHandle
    status: ExecutionStatus
    result: Result | None = None
    reason: str = ""
    completed_at: float | None = None


class ExecutionBackend(Protocol):
    """start must deduplicate identical requests and reject conflicting reuse.

    A transport failure is unknown, never evidence of termination. completed
    means the entire owned execution is quiescent, not merely its client exited.
    cancel requests termination; only reconcile can confirm it.
    """

    id: str

    def start(self, request: ExecutionRequest) -> ExecutionHandle: ...

    def reconcile(self, handle: ExecutionHandle) -> ExecutionObservation: ...

    def cancel(self, handle: ExecutionHandle) -> None: ...


def validate_observation(expected: ExecutionHandle, observation: ExecutionObservation) -> None:
    if observation.handle != expected:
        raise ValueError("Stale or foreign execution observation")
    if observation.status not in ("running", "completed", "terminated", "unknown"):
        raise ValueError("Unknown execution status")
    result = observation.result
    if observation.status == "completed":
        if (
            result is None
            or observation.completed_at is None
            or not math.isfinite(observation.completed_at)
        ):
            raise ValueError("Completion requires a result")
        if result.attempt_id != expected.id or result.generation != expected.generation:
            raise ValueError("Result belongs to another attempt")
    elif result is not None or observation.completed_at is not None:
        raise ValueError("Only confirmed completion can carry a result")
