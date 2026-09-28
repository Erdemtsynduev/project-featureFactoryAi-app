"""Where features come from: Markdown plans today; trackers or the operator tomorrow."""

from pathlib import Path
from typing import Protocol

from sdd_factory.sources.markdown import PlanDocument


class FeatureSource(Protocol):
    """A source of plan documents: each becomes one feature over its open rows."""

    def documents(self, workspace: Path) -> list[PlanDocument]: ...
