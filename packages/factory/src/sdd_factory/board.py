"""The factory's decisions over its board, as pure functions of what was read.

Services read runs and task records, call a rule here, and apply what it decides;
no rule here reads or writes anything.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sdd_core import machine
from sdd_core.models import Run

from sdd_factory.model import TaskRecord


@dataclass(frozen=True)
class Replan:
    """What rebuilding a board removes, and the features whose rows are planned again."""

    removed: frozenset[str]
    reopened: frozenset[str]


def replan(
    records: Mapping[str, TaskRecord],
    runs: Mapping[str, Run],
    edges: Iterable[tuple[str, str]],
    project: str,
    plan: str = "",
) -> Replan:
    """Never-started plan work of `project` (or of one `plan`) to plan again.

    Removed: a plan's never-started features and the tickets of an open plan feature;
    never anything a kept run depends on. A feature losing tickets is reopened: its rows
    are planned again, and its never-started reviews go with it.
    """
    edges = tuple(edges)

    def planned(item: TaskRecord) -> bool:
        """A plan's feature, or a ticket of an open plan feature."""
        if item.kind == "feature":
            return True
        parent = records.get(item.parent)
        return item.kind == "ticket" and parent is not None and bool(parent.rows)

    candidates = {
        key
        for key, item in records.items()
        if key in runs
        and item.project == project
        and item.plan
        and (not plan or item.plan == plan)
        and planned(item)
        and not item.reviews
        and not records.get(item.parent, item).closed
        and machine.discardable(runs[key])
    }
    while True:
        kept_needs = {needed for key, needed in edges if key not in candidates}
        if not candidates & kept_needs:
            break
        candidates -= kept_needs
    reopened = frozenset(
        item.parent
        for key, item in records.items()
        if key in candidates and item.kind == "ticket" and item.parent not in candidates
    )
    # A review that never started goes with its plan; nothing depends on reviews.
    candidates |= {
        key
        for key, item in records.items()
        if key in runs and item.reviews in reopened and machine.discardable(runs[key])
    }
    return Replan(frozenset(candidates), reopened)


def review_trigger(run: Run, blocked_by_agent: bool) -> tuple[str, str] | None:
    """Why a stopped ticket needs its plan reviewed: its agent blocked it, or its repair
    loop exhausted a step's visits. Engine or operator blocks never do."""
    if run.status != "blocked" or run.previous_attempt is None:
        return None
    if run.cause == "visit_limit":
        return "limit", "exhausted its repair loop"
    if run.cause == "blocked" and blocked_by_agent:
        return "blocked", "was blocked by its agent"
    return None


def plan_tickets(records: Mapping[str, TaskRecord], runs: Iterable[str], plan: str) -> set[str]:
    """The plan's existing tickets a new ticket may wait for (`after`): superseded work is
    not carried on, so nothing may wait for it."""
    existing = set(runs)
    return {
        key
        for key, item in records.items()
        if key in existing
        and plan
        and item.kind == "ticket"
        and item.plan == plan
        and not item.superseded
    }
