"""Role interfaces of the persistence transaction; each consumer names only what it uses.

A backend implements all of them on one unit of work (see `ports.UnitOfWork`), so
every role participates in the same atomic commit.
"""

from dataclasses import dataclass
from typing import Protocol

from sdd_core.models import Result, Run, Transition


class Conflict(ValueError):
    pass


class StaleVersion(Conflict):
    """Optimistic concurrency: the run changed since it was read; re-read and retry."""


@dataclass(frozen=True)
class EffectRecord:
    id: str
    run_id: str
    kind: str
    status: str
    workspace: str
    pid: int | None = None
    created: float | None = None
    host_nonce: str | None = None
    receipt: str | None = None
    external: bool = False


class RunRecords(Protocol):
    """Run projections, their inputs and CAS-protected transitions."""

    def runs(self) -> tuple[Run, ...]: ...
    def run(self, identifier: str) -> Run: ...
    def apply(self, before: Run, transition: Transition) -> Run: ...
    def context(self, run_id: str) -> str: ...
    def set_context(self, identifier: str, context: str) -> None: ...
    def location(self, identifier: str) -> tuple[str, str]: ...
    def locations(self) -> tuple[tuple[str, str], ...]:
        """(run id, workspace) of every run, in one read."""
        ...

    def relocate(self, identifier: str, workspace: str, claim: str) -> None: ...
    def runnable(self) -> tuple[str, ...]:
        """Resumed, unfinished runs whose prerequisites are all accepted, fairest first."""
        ...

    def last_transition(self) -> float | None: ...
    def queue_usage(self) -> tuple[int, int]:
        """Model calls and planning calls reserved by all runs."""
        ...


class CommandLog(Protocol):
    """Idempotent operator commands keyed by request id."""

    def command(self, identifier: str) -> tuple[str, str] | None: ...
    def save_command(self, identifier: str, request: str, response: str) -> None: ...


class AdmissionRecords(Protocol):
    """Facts that decide whether a run may take a process slot and its paths."""

    def policy(self, workspace: str) -> tuple[str, ...]: ...
    def set_policy(self, workspace: str, mandatory: tuple[str, ...]) -> None: ...
    def dependencies(self, identifier: str) -> tuple[Run, ...]: ...
    def dependency_edges(self) -> tuple[tuple[str, str], ...]:
        """(run, prerequisite) of every dependency, in one read."""
        ...

    def set_dependencies(self, identifier: str, prerequisites: tuple[str, ...]) -> None:
        """Replace a run's prerequisites; the caller has checked they form no cycle."""
        ...

    def active_claims(self) -> tuple[tuple[str, str], ...]: ...
    def unfinished_claims(self, identifier: str) -> tuple[tuple[Run, str], ...]:
        """Other started, unaccepted runs with their claims."""
        ...


class ResultRecords(Protocol):
    """Receipts of processed results, keyed by attempt."""

    def results(self, identifier: str) -> tuple[Result, ...]: ...
    def recent_results(self, run_id: str, limit: int) -> tuple[str, ...]: ...
    def step_results(self, run_id: str) -> tuple[tuple[str, str], ...]:
        """(step id, result document) of every processed attempt, newest first."""
        ...

    def attempt_bases(self, run_id: str) -> tuple[tuple[str, str], ...]:
        """(step id, base revision) of every dispatched attempt, processed or not, oldest first."""
        ...

    def receipt(self, attempt: str) -> tuple[str, str] | None: ...
    def save_result(self, attempt: str, fingerprint: str, document: str) -> None: ...
    def effect_status(self, attempt: str, status: str) -> None: ...


class ExecutionRecords(Protocol):
    """Outbox effects, pinned handlers and host ownership of attempts."""

    def bind_handler(self, run_id: str, handler: str, manifest: str) -> None:
        """Pin a handler's manifest to a run.

        A different manifest is a Conflict once the run has dispatched any process
        attempt; before that the pin follows the operator's current profiles.
        """
        ...

    def effect(self, attempt: str) -> EffectRecord: ...
    def effects(self, statuses: tuple[str, ...]) -> tuple[EffectRecord, ...]: ...
    def bind_execution(self, run_id: str, attempt: str, backend: str, document: str) -> None: ...
    def execution(self, attempt: str) -> tuple[str, str] | None: ...


class PortfolioRecords(Protocol):
    def bind_portfolio(self, identifier: str, document: str) -> None: ...
    def portfolio(self, identifier: str) -> str | None: ...


class LaneRecords(Protocol):
    """Isolated working copies ("lanes"): one JSON document per run, owned by runtime."""

    def lane(self, run_id: str) -> str | None: ...
    def save_lane(self, run_id: str, document: str) -> None: ...
