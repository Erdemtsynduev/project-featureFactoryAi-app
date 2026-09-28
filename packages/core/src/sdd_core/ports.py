"""Application persistence ports; no SQL or backend types cross this boundary."""

from contextlib import AbstractContextManager
from typing import Protocol

from sdd_core.models import Result, Run, Workflow
from sdd_core.records import (
    AdmissionRecords,
    CommandLog,
    Conflict,
    ResultRecords,
    StaleVersion,
)
from sdd_core.runtime_ports import RuntimeRecords

__all__ = ["Conflict", "StaleVersion", "StateStore", "UnitOfWork", "Workspace"]


class UnitOfWork(RuntimeRecords, CommandLog, AdmissionRecords, ResultRecords, Protocol):
    """One atomic transaction exposing every record role (see `sdd_core.records`)."""


class StateStore(Protocol):
    def unit(self) -> AbstractContextManager[UnitOfWork]: ...
    def workflow(self, identifier: str) -> Workflow: ...
    def get(self, identifier: str) -> Run: ...
    def create(
        self,
        run: Run,
        workspace: str,
        context: str,
        claim: str,
        now: float,
        dependencies: tuple[str, ...] = (),
    ) -> Run: ...


class Workspace(Protocol):
    """Claims name the owned paths of a run; the whole workspace unless scoped."""

    def resolve(self, path: str) -> str: ...
    def claim(self, root: str, scope: tuple[str, ...]) -> str: ...
    def paths(self, claim: str) -> tuple[str, ...]: ...
    def overlaps(self, left: str, right: str) -> bool: ...
    def normalize(self, run_id: str, result: Result, workspace: str) -> Result: ...
    def verify(self, result: Result, workspace: str, revision: str) -> None: ...
