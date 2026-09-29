"""Mirror the factory into a project's tracker: state-driven, recorded, delivered in order.

`observe` compares what the tracker should show with the newest recorded
publication of each item and records only what changed:

- a feature's approved specification;
- the tickets approved for it, created in the tracker as children of the feature;
- the state of the feature and of each mirrored ticket (queued, working, needs a
  person, blocked, done).

Only items that came from the tracker (they carry a `link`) are mirrored; tickets
get their link when the tracker created them. `deliver` sends recorded publications
oldest first. A failure keeps the order: that publication is retried later with a
growing delay, and nothing newer of the project jumps ahead of it. Observing never
contacts the tracker, so a tracker that is down only delays what people see.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from sdd_core.catalog import OutboxEntry
from sdd_core.codec import canonical, digest, text
from sdd_core.models import Json
from sdd_core.tracking import (
    TicketMirror,
    Tracker,
    TrackerUpdate,
    mirror_state,
    receipt_json,
    update_json,
    update_load,
)

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.journal import FlightLog
from sdd_factory.model import TaskRecord, ticket_places

RETRY_SECONDS = 30
RETRY_MAX_SECONDS = 3600
# A publication that keeps failing is given up after this many attempts; its error
# stays visible and newer publications continue.
MAX_ATTEMPTS = 12
REASON_CHARS = 500


@dataclass(frozen=True)
class RunView:
    """What the mirror needs of a run: engine status and the board's attention."""

    id: str
    status: str
    attention: str
    reason: str = ""


class TrackerSync:
    def __init__(
        self,
        catalog: ProjectCatalog,
        trackers: Callable[[dict[str, Json]], Tracker | None],
        log: FlightLog,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`trackers(project)` gives the project's configured tracker, or None."""
        self.catalog, self.trackers, self.log, self.clock = catalog, trackers, log, clock

    # Observing -----------------------------------------------------------------

    def observe(self, project_id: str, views: Iterable[RunView]) -> int:
        """Record what changed for the project's mirrored items; returns how many."""
        records = self.catalog.tasks()
        recorded = 0
        for view in views:
            item = records.get(view.id)
            if item is None or item.project != project_id:
                continue
            if item.link:
                state = mirror_state(view.status, view.attention)
                reason = view.reason[:REASON_CHARS] if state in ("blocked", "needs_person") else ""
                recorded += self._record(
                    project_id, TrackerUpdate("state", view.id, item.link, reason, state)
                )
            if item.link and item.kind == "feature":
                recorded += self._documents(project_id, view.id, item, records)
        return recorded

    def _documents(
        self, project_id: str, run: str, item: TaskRecord, records: dict[str, TaskRecord]
    ) -> int:
        stored = self.catalog.artifacts(run)
        recorded = 0
        if "specification" in stored:
            recorded += self._record(
                project_id,
                TrackerUpdate("specification", run, item.link, stored["specification"].content),
            )
        breakdown = stored.get("tickets")
        if breakdown is not None:
            children = {key for key, child in records.items() if child.parent == run}
            tickets = mirrors(breakdown.data, children)
            if tickets and any(not records[t.run].link for t in tickets):
                recorded += self._record(
                    project_id, TrackerUpdate("tickets", run, item.link, tickets=tickets)
                )
        return recorded

    def _record(self, project_id: str, update: TrackerUpdate) -> int:
        """Record `update` unless it is what was last recorded for its item."""
        last = self.catalog.records.last_update(update.run, update.kind)
        if last is not None and update_load(last.document).id == update.id:
            return 0
        # Chained to the previous publication, so a state that returns is shown again.
        identifier = digest(canonical([last.id if last else "", update.id]))[:32]
        entry = OutboxEntry(identifier, project_id, update.run, update.kind, update_json(update))
        return int(self.catalog.records.record_update(entry))

    # Delivering ----------------------------------------------------------------

    def deliver(self, project: dict[str, Json], limit: int = 20) -> int:
        """Send due publications of the project in order; returns how many were sent."""
        project_id = text(project.get("id"), "id")
        pending = self.catalog.records.pending_updates(project_id)
        if not pending:
            return 0
        tracker = self.trackers(project)
        if tracker is None:
            return 0
        sent = 0
        for entry in pending[:limit]:
            now = self.clock()
            if entry.next_at > now:
                break
            try:
                receipt = tracker.publish(update_load(entry.document))
            except Exception as error:
                self._failed(entry, error, now)
                if entry.attempts + 1 < MAX_ATTEMPTS:
                    break  # keep the order: nothing newer goes first
                continue
            for run, link in receipt.links:
                self._link(run, link)
            self.catalog.records.settle_update(
                replace(
                    entry,
                    status="done",
                    attempts=entry.attempts + 1,
                    error="",
                    receipt=receipt_json(receipt),
                )
            )
            sent += 1
        if sent:
            self.log.record("tracker_published", project=project_id, count=sent)
        return sent

    def _failed(self, entry: OutboxEntry, error: Exception, now: float) -> None:
        attempts = entry.attempts + 1
        given_up = attempts >= MAX_ATTEMPTS
        message = f"{type(error).__name__}: {error}"[:REASON_CHARS]
        self.catalog.records.settle_update(
            replace(
                entry,
                status="failed" if given_up else "pending",
                attempts=attempts,
                next_at=now + min(RETRY_MAX_SECONDS, RETRY_SECONDS * 2 ** (attempts - 1)),
                error=message,
            )
        )
        self.log.record(
            "tracker_failed",
            "error" if given_up else "warning",
            run=entry.run,
            update=entry.kind,
            attempts=attempts,
            error=message,
        )

    def _link(self, run: str, link: str) -> None:
        record = self.catalog.tasks().get(run)
        if record is not None and record.link != link:
            self.catalog.update_task(run, record.changed(link=link))


def mirrors(breakdown: Json, admitted: set[str]) -> tuple[TicketMirror, ...]:
    """The admitted tickets of a feature's breakdown, as a tracker shows them."""
    places = ticket_places(breakdown)
    found: list[TicketMirror] = []
    for item in breakdown if isinstance(breakdown, list) else []:
        if not isinstance(item, dict) or item.get("run") not in admitted:
            continue
        run = str(item["run"])
        place = places.get(run, {})
        after, wave, acceptance = place.get("after"), place.get("wave"), item.get("acceptance")
        found.append(
            TicketMirror(
                run,
                str(item.get("id", "")),
                str(item.get("title", "")),
                str(item.get("goal", "")),
                tuple(str(x) for x in acceptance) if isinstance(acceptance, list) else (),
                tuple(str(x) for x in after) if isinstance(after, list) else (),
                wave if isinstance(wave, int) else 1,
                place.get("hitl") is True,
            )
        )
    return tuple(found)
