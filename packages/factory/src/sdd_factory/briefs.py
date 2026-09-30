"""Briefs of planning runs: what a draft's lead and a feature's analyst are given.

Pure text builders. A brief carries its scope itself (the rows, the goals and rules
around them), so planning does not depend on re-reading a source that may have moved
on, and lists the work already cut from the same source so nothing is planned twice.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sdd_core.tickets import TicketDraft
from sdd_core.tracking import WorkItem, WorkRow
from sdd_runtime.lane_model import lane_branch

from sdd_factory.model import TaskRecord

BODY_CHARS = 6000
ROW_CHARS = 400
LINE_CHARS = 240


@dataclass(frozen=True)
class SourceWork:
    """Tickets that already came from a source, as a brief lists them."""

    queued: tuple[str, ...] = ()
    delivered: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()


def source_work(
    records: Mapping[str, TaskRecord], status: Mapping[str, str], keys: Iterable[str]
) -> SourceWork:
    """The tickets `keys`: still queued (with their state), delivered, and superseded
    (their scope is planned again; their lane branch keeps their work)."""
    queued: list[str] = []
    delivered: list[str] = []
    superseded: list[str] = []
    for key in sorted(keys):
        item = records[key]
        if status[key] == "accepted":
            delivered.append(f"{key} — {item.title}")
        elif item.superseded:
            superseded.append(f"{key} — {item.title} (branch {lane_branch(key)})")
        else:
            queued.append(f"{key} — {item.title} ({status[key]})")
    return SourceWork(tuple(queued), tuple(delivered), tuple(superseded))


NO_WORK = SourceWork()


def _origin(item: WorkItem) -> list[str]:
    where = (
        f"{item.path} (a file in the workspace; read it only for detail the rows lack)"
        if item.path
        else f"{item.url or item.link} (tracker)"
        if item.url or item.link
        else ""
    )
    lines = [f"Source: {where}."] if where else []
    if item.body:
        lines += ["Goals, rules and findings of the draft:", item.body[:BODY_CHARS]]
    return lines


def _rows(rows: Iterable[WorkRow]) -> list[str]:
    lines = []
    for row in rows:
        mark = "partial - continue the recorded work, do not restart" if row.mark == "~" else "open"
        # Wrapped rows continue on indented lines; evidence notes stay in the source.
        text = " ".join([row.text, *row.detail])
        lines.append(f"- {row.id} ({mark}): {text[:ROW_CHARS]}")
    return lines


def _work(work: SourceWork) -> list[str]:
    sections = (
        (
            work.queued,
            "Already queued tickets from this source (do not duplicate them; a new ticket "
            "that waits for one names its run id in `after`):",
        ),
        (
            work.delivered,
            "Delivered tickets from this source (done; build on them, do not redo them):",
        ),
        (
            work.superseded,
            "Superseded tickets from this source (plan their scope again, one repository per"
            " ticket; reuse the partial work on their lane branch):",
        ),
    )
    lines: list[str] = []
    for items, heading in sections:
        if items:
            lines += ["", heading, *(f"- {item[:LINE_CHARS]}" for item in items)]
    return lines


def draft_brief(item: WorkItem, work: SourceWork = NO_WORK, labels: Iterable[str] = ()) -> str:
    """What the lead cuts into features: the draft's text, its open rows, the work
    already started from its source and the labels the project uses."""
    lines = [f"Draft: {item.title}.", *_origin(item), ""]
    if item.rows:
        lines += ["Open rows of the draft, its scope (id, mark, text):", *_rows(item.rows)]
    else:
        lines.append("The draft has no rows: its text is the scope.")
    lines += _work(work)
    known = sorted(labels)
    if known:
        lines += ["", "Labels this project already uses: " + ", ".join(known)]
    return "\n".join(lines)


def feature_brief(item: WorkItem, feature: TicketDraft, work: SourceWork = NO_WORK) -> str:
    """What a feature's analyst specifies: the feature the lead cut, the rows of the
    draft it covers (with the draft's goals and rules) and the tickets from the same
    source."""
    lines = [f"Feature {feature.id}: {feature.title}"]
    if feature.goal:
        lines.append("Goal: " + feature.goal)
    if feature.labels:
        lines.append("Labels: " + ", ".join(feature.labels))
    lines += ["", f"Cut from the draft: {item.title}.", *_origin(item)]
    rows = [row for row in item.rows if row.id in feature.covers]
    if rows:
        lines += ["", "Scope: these rows of the draft (id, mark, text):", *_rows(rows)]
    return "\n".join([*lines, *_work(work)])
