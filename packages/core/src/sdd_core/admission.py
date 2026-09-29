"""Admission policy: whether one more attempt may start beside those already running.

These are pure rules over counts the store reports; the application reads the
counts inside its transaction and asks these objects for the decision.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

type Slot = Literal["agent", "operation"]


def slot(kind: str) -> Slot:
    """Agents share the agent slots; every other process kind shares the operation slots."""
    return "agent" if kind == "agent" else "operation"


@dataclass(frozen=True)
class Slots:
    """How many agent and non-agent processes may run at once."""

    agents: int = 2
    operations: int = 1

    def __post_init__(self) -> None:
        if self.agents < 1 or self.operations < 1:
            raise ValueError("At least one agent and one operation slot are required")

    def free(self, kind: str, running: Iterable[str]) -> bool:
        """Whether a `kind` attempt fits beside attempts of the `running` kinds."""
        wanted = slot(kind)
        taken = sum(slot(other) == wanted for other in running)
        return taken < (self.agents if wanted == "agent" else self.operations)


MAX_QUEUE_CALLS = 100000


@dataclass(frozen=True)
class QueueBudget:
    """An optional cap on the model calls the whole queue may reserve; None is no cap.

    A cap fits agents billed per token (API keys). Subscriptions are not paced by
    calls: their windows are (see `sdd_usage.quota`), so their queues run uncapped.
    """

    calls: int | None = None
    planning_calls: int | None = None

    def __post_init__(self) -> None:
        if any(value is not None and value < 0 for value in (self.calls, self.planning_calls)):
            raise ValueError("Queue call budgets cannot be negative")
        if self.calls is not None and self.calls > MAX_QUEUE_CALLS:
            raise ValueError(f"Queue call budget cannot exceed {MAX_QUEUE_CALLS}")
        total, planning = self.calls, self.planning_calls
        if total is not None and planning is not None and planning > total:
            raise ValueError("Planning calls are part of all calls: planning cannot exceed total")

    def exhausted(self, calls: int, planning_calls: int, planning: bool) -> bool:
        return (self.calls is not None and calls >= self.calls) or (
            planning and self.planning_calls is not None and planning_calls >= self.planning_calls
        )
