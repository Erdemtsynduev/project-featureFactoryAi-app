"""Ending work early: close a parent with what is done, or hand work to the feature that plans it again."""

import time

from sdd_core.codec import text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.tickets import (
    strings,
)
from sdd_runtime.engine import Engine

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    unfinished,
)


def supersede_run(engine: Engine, catalog: ProjectCatalog, key: str, by: str) -> bool:
    """Mark unfinished work `key` as taken over by `by` and pause it; False when it
    already was. Its lane stays: the successor's tickets may reuse its work."""
    item, run = catalog.task(key), engine.store.get(key)
    if item.superseded or run.status == "accepted":
        return False
    catalog.update_task(key, item.changed(superseded=by))
    if run.active is None and not run.paused and run.status != "blocked":
        engine.command(key, "pause", f"{key}:superseded", run.version, time.time())
    return True


class Supersession:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        log: FlightLog,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.log = log

    def supersede(self, doc: dict[str, Json]) -> dict[str, object]:
        """Hand unfinished work (`ids`) over to the feature `by` that plans its scope
        again: each run is paused and shown as superseded; its lane and branch stay, so
        the new tickets can reuse the partial work. Nothing is removed."""
        by = text(doc.get("by"), "by")
        successor = self.catalog.task(by)
        if successor.kind != "feature" or successor.closed:
            raise ValueError("Work is superseded by an open feature")
        identifiers = strings(doc.get("ids"), "id")
        for identifier in identifiers:
            item, run = self.catalog.task(identifier), self.engine.store.get(identifier)
            if item.source != successor.source or item.reviews:
                raise ValueError(f"{identifier} is not work from the source {by} plans")
            if run.status == "accepted" or run.active is not None:
                raise Conflict(f"{identifier} is accepted or has a live attempt")
        superseded = [
            key for key in identifiers if supersede_run(self.engine, self.catalog, key, by)
        ]
        self.log.record("work_superseded", by=by, runs=list[Json](superseded))
        return {"by": by, "superseded": list[Json](superseded)}

    def close(self, doc: dict[str, Json]) -> dict[str, object]:
        """Close a parent before all its children are done: it counts as delivered
        with what is done, and each unfinished child becomes top-level work that
        remembers where it came from. Nothing runs, stops or is removed."""
        identifier = text(doc.get("id"), "id")
        if self.engine.store.get(identifier).status != "accepted":
            raise ValueError("Only work whose own run is accepted can be closed")
        records = self.catalog.tasks()
        children = [key for key, item in records.items() if item.parent == identifier]
        if not children:
            raise ValueError("Only work with tickets can be closed")
        with self.engine.store.unit() as db:
            status = {run.id: run.status for run in db.runs()}
        detached = self.catalog.close_task(identifier, unfinished(identifier, records, status))
        self.log.record("work_closed", run=identifier, detached=list[Json](detached))
        return {"closed": identifier, "detached": list[Json](detached)}
