"""The factory's decisions over its board, as pure functions of what was read.

Services read runs and task records, call a rule here, and apply what it decides;
no rule here reads or writes anything.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from sdd_core import machine
from sdd_core.models import Run

from sdd_factory.model import TaskRecord


@dataclass(frozen=True)
class Replan:
    """What rebuilding a board removes, and the parents whose rows are taken in again."""

    removed: frozenset[str]
    reopened: frozenset[str]


def replan(
    records: Mapping[str, TaskRecord],
    runs: Mapping[str, Run],
    edges: Iterable[tuple[str, str]],
    project: str,
    sources: Collection[str] | None = None,
) -> Replan:
    """Never-started work of `project` that came from a source (from one of `sources`,
    when given), to take in again.

    Removed: never-started drafts and features, and the tickets of an open feature;
    never anything a kept run depends on. A parent losing children is reopened: its
    rows are taken in again, and its never-started reviews go with it.
    """
    edges = tuple(edges)

    def planned(item: TaskRecord) -> bool:
        """A draft, a feature, or a ticket of a feature."""
        if item.kind in ("draft", "feature"):
            return True
        parent = records.get(item.parent)
        return item.kind == "ticket" and parent is not None and parent.kind == "feature"

    candidates = {
        key
        for key, item in records.items()
        if key in runs
        and item.project == project
        and item.source
        and (sources is None or item.source in sources)
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
        if key in candidates and item.parent and item.parent not in candidates
    )
    # A review that never started goes with its feature; nothing depends on reviews.
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


def source_tickets(records: Mapping[str, TaskRecord], runs: Iterable[str], source: str) -> set[str]:
    """The existing tickets that came from `source`: what a brief lists."""
    existing = set(runs)
    return {
        key
        for key, item in records.items()
        if key in existing and source and item.kind == "ticket" and item.source == source
    }


def live_tickets(records: Mapping[str, TaskRecord], runs: Iterable[str], source: str) -> set[str]:
    """The tickets from `source` a new ticket may wait for (`after`): superseded work is
    not carried on, so nothing may wait for it."""
    found = source_tickets(records, runs, source)
    return {key for key in found if not records[key].superseded}


def below(records: Mapping[str, TaskRecord], root: str) -> set[str]:
    """Everything cut from `root`, at any depth."""
    children: dict[str, list[str]] = {}
    for key, item in records.items():
        children.setdefault(item.parent, []).append(key)
    found: set[str] = set()
    frontier = [root]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found
