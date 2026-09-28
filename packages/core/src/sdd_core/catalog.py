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
        """First write wins: task labels are fixed at creation."""
        ...

    def preference(self, key: str) -> str | None: ...
    def save_preference(self, key: str, document: str) -> None: ...
    def agent_calls(self) -> tuple[AgentCall, ...]: ...
    def daily_dispatches(self, days: int) -> tuple[tuple[str, int], ...]:
        """Newest `days` UTC days with dispatch counts."""
        ...
