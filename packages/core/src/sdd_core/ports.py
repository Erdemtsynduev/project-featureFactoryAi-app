"""Application persistence ports; no SQL or backend types cross this boundary."""

from contextlib import AbstractContextManager
from typing import Protocol

from sdd_core.models import Result, Run, Workflow
from sdd_core.records import (
    AdmissionRecords,
    CommandLog,
    Conflict,
    ExecutionRecords,
    LaneRecords,
    PortfolioRecords,
    ResultRecords,
    RunRecords,
    StaleVersion,
)

__all__ = ["Conflict", "StaleVersion", "StateStore", "UnitOfWork", "Workspace"]


class UnitOfWork(
    RunRecords,
    CommandLog,
    AdmissionRecords,
    ResultRecords,
    ExecutionRecords,
    PortfolioRecords,
    LaneRecords,
    Protocol,
):
    """One atomic transaction exposing every record role (see `sdd_core.records`).

    Consumers name the narrowest role they use; a backend implements them all on one
    transaction so every role takes part in the same commit.
    """


class StateStore(Protocol):
    def unit(self) -> AbstractContextManager[UnitOfWork]: ...
    def workflow(self, identifier: str) -> Workflow: ...
    def publish(self, workflow: Workflow, mandatory: tuple[str, ...] = ()) -> str:
        """Validate and store an immutable workflow version; returns its digest."""
        ...

    def get(self, identifier: str) -> Run:
        """A snapshot read outside any transaction, for display and pre-checks; a
        decision that writes re-reads the run inside its unit of work."""
        ...

    def create(
        self,
        run: Run,
        workspace: str,
        context: str,
        claim: str,
        now: float,
        dependencies: tuple[str, ...] = (),
    ) -> Run: ...
    def discard(self, identifiers: tuple[str, ...]) -> tuple[str, ...]:
        """Remove runs that never dispatched, atomically; refuse anything else."""
        ...


class Workspace(Protocol):
    """Claims name the owned paths of a run; the whole workspace unless scoped."""

    def resolve(self, path: str) -> str: ...
    def claim(self, root: str, scope: tuple[str, ...]) -> str: ...
    def paths(self, claim: str) -> tuple[str, ...]: ...
    def overlaps(self, left: str, right: str) -> bool: ...
    def normalize(self, run_id: str, result: Result, workspace: str) -> Result: ...
    def verify(self, result: Result, workspace: str, revision: str) -> None: ...

    def folder(self, run_id: str, attempt_id: str = "") -> str:
        """Where the engine keeps the run's own files, or one attempt's: packets, logs,
        receipts. Never inside a project."""
        ...

    def lane(self, run_id: str) -> str:
        """Where the run's isolated working copy is kept. Never inside a project."""
        ...
