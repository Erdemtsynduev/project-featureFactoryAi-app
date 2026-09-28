"""Durable runtime records. Implementations participate in the same state transaction."""

from typing import Protocol

from sdd_core.records import (
    EffectRecord,
    ExecutionRecords,
    LaneRecords,
    PortfolioRecords,
    RunRecords,
)

__all__ = ["EffectRecord", "RuntimeRecords"]


class RuntimeRecords(
    RunRecords,
    ExecutionRecords,
    PortfolioRecords,
    LaneRecords,
    Protocol,
):
    """Runtime-owned records; kept as one name for existing backends."""

    # Role members used by runtime maintenance and the admission policy.
    def recent_results(self, run_id: str, limit: int) -> tuple[str, ...]: ...
    def set_policy(self, workspace: str, mandatory: tuple[str, ...]) -> None: ...
