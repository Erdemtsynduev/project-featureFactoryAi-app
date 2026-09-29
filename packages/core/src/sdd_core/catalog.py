"""Port for application-owned metadata beside the engine: projects, plans, task labels.

Documents are canonical JSON owned by the application; the backend stores them
opaquely and reports measured agent calls without exposing its schema.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentCall:
    """One dispatched agent attempt and its processed result, when there is one."""

    run_id: str
    workflow_digest: str
    step: str
    started: float | None
    status: str
    result: str | None


@dataclass(frozen=True)
class OutboxEntry:
    """A recorded publication to a project's tracker and how its delivery went.

    `status` is `pending` until delivered (`done`) or given up (`failed`); a pending
    entry is not retried before `next_at`.
    """

    id: str
    project: str
    run: str
    kind: str
    document: str
    status: str = "pending"
    attempts: int = 0
    next_at: float = 0.0
    error: str = ""
    receipt: str = ""


class CatalogRecords(Protocol):
    def projects(self) -> tuple[str, ...]: ...
    def save_project(self, identifier: str, document: str) -> None: ...
    def plans(self) -> tuple[tuple[str, str], ...]:
        """(project, document) pairs ordered by project and plan id."""
        ...

    def save_plans(self, project: str, plans: tuple[tuple[str, str], ...]) -> None:
        """Upsert (plan id, document) pairs of one project atomically."""
        ...

    def tasks(self) -> tuple[tuple[str, str], ...]: ...
    def save_task(self, identifier: str, document: str) -> None:
        """Create a task record; an existing record is kept (creation is idempotent)."""
        ...

    def update_task(self, identifier: str, document: str) -> None:
        """Replace an existing task record; unknown ids are an error."""
        ...

    def artifacts(self, run: str) -> tuple[tuple[str, str], ...]:
        """(kind, document) pairs a run produced, such as its specification."""
        ...

    def save_artifact(self, run: str, kind: str, document: str) -> None:
        """Upsert one artifact of a run by kind."""
        ...

    def preference(self, key: str) -> str | None: ...
    def save_preference(self, key: str, document: str) -> None: ...
    def agent_calls(self) -> tuple[AgentCall, ...]: ...
    def daily_dispatches(self, days: int) -> tuple[tuple[str, int], ...]:
        """Newest `days` UTC days with dispatch counts."""
        ...

    # Tracker outbox: publications are recorded before delivery, delivered in order.

    def record_update(self, entry: OutboxEntry) -> bool:
        """Record a pending publication; False when its id was recorded before."""
        ...

    def last_update(self, run: str, kind: str) -> OutboxEntry | None:
        """The newest publication of this kind about a run, delivered or not."""
        ...

    def pending_updates(self, project: str) -> tuple[OutboxEntry, ...]:
        """Undelivered publications of a project, oldest first."""
        ...

    def settle_update(self, entry: OutboxEntry) -> None:
        """Store a delivery outcome: status, attempts, next try, error and receipt."""
        ...

    def updates(self, project: str, limit: int) -> tuple[OutboxEntry, ...]:
        """The newest publications of a project, for people to inspect."""
        ...
