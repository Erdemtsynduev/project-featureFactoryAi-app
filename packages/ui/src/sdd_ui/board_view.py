"""The board's read model: what the console shows of runs, tasks and settings.

It only reads; actions go through `WorkspaceService.mutate`.
"""

from functools import cache, partial
from typing import TYPE_CHECKING

from sdd_core.codec import encode, object_json
from sdd_core.models import Json
from sdd_core.questions import questions
from sdd_factory.diagnostics import live, record
from sdd_factory.model import ticket_places
from sdd_factory.settings import ROLE_DEFAULTS

from sdd_ui.attention import attention, calls_needed, lane, outline, projection, rollup

if TYPE_CHECKING:
    from sdd_ui.service import WorkspaceService


class BoardView:
    def __init__(self, service: "WorkspaceService") -> None:
        """Reads through the service's components; built once they exist."""
        self.engine = service.engine
        self.catalog = service.catalog
        self.queue = service.queue
        self.agents = service.agents
        self.flows = service.flows
        self.documents = service.documents
        self.reviews = service.reviews
        self.log = service.log
        self.versions = service.versions
        self.tracker_kinds = service.tracker_kinds

    def runs(self) -> list[dict[str, object]]:
        """One projection per run with what the views show, derived on the server
        (lane, attention, waits, calls still needed), newest first."""
        with self.engine.store.unit() as unit:
            runs = unit.runs()
            edges = unit.dependency_edges()
        status = {run.id: run.status for run in runs}
        prerequisites: dict[str, list[str]] = {}
        for run_id, needed in edges:
            prerequisites.setdefault(run_id, []).append(needed)
        pending = {
            run.id: tuple(d for d in prerequisites.get(run.id, ()) if status[d] != "accepted")
            for run in runs
            if run.status != "accepted"
        }
        definitions = self.engine.store.definitions()
        workflows = dict(definitions)
        profiles = {manifest.id for manifest in self.agents.handlers.manifests()}
        available = cache(self.agents.available)
        running = bool(self.queue.settings["running"]) and not self.queue.error
        records = self.catalog.tasks()
        own = {
            run.id: attention(
                run,
                workflows[run.workflow_digest].step(run.step),
                queue_running=running,
                pending=pending.get(run.id, ()),
                has_profile=profiles.__contains__,
                available=available,
                superseded=records[run.id].superseded if run.id in records else "",
                held=partial(self.engine.holders, run.id),
            )
            for run in runs
        }
        # Work is a tree: a parent's reason and progress come from its children.
        children: dict[str, list[str]] = {}
        for run in runs:
            parent = records[run.id].parent if run.id in records else ""
            if parent in own:
                children.setdefault(parent, []).append(run.id)
        closed = frozenset(key for key, item in records.items() if item.closed)
        places: dict[str, dict[str, Json]] = {}
        for parent in children:
            breakdown = self.catalog.artifacts(parent).get("tickets")
            if breakdown is not None:
                places.update(ticket_places(breakdown.data))
        derived, progress = rollup(
            own, {key: tuple(value) for key, value in children.items()}, closed
        )
        reasons = {key: encode(value) for key, value in derived.items()}
        return [
            {
                **projection(run),
                "attention": reasons[run.id],
                "lane": lane(str(reasons[run.id]["tone"])),
                "needs": calls_needed(run, workflows[run.workflow_digest]),
                "pending_dependencies": list(pending.get(run.id, ())),
                "dependencies": prerequisites.get(run.id, []),
                "progress": encode(progress[run.id]) if run.id in progress else None,
                # Its place in the parent's plan: number, wave, what it follows.
                "ticket": places.get(run.id),
            }
            for run in reversed(runs)
        ]

    def state(self) -> dict[str, object]:
        """The board's read model; the task drawer reads a run in full through `detail`."""
        with self.engine.store.unit() as unit:
            runs = unit.runs()
            last = unit.last_transition()
            locations = dict(unit.locations())
        definitions = self.engine.store.definitions()
        return {
            "runs": self.runs(),
            "versions": self.versions,
            "trackers": list(self.tracker_kinds),
            "totals": {
                "calls": sum(r.spend.calls for r in runs),
                "planning_calls": sum(r.spend.planning_calls for r in runs),
                "tokens": sum(r.spend.tokens for r in runs),
                "usage_unknown": any(r.spend.usage_unknown for r in runs),
                "accepted": sum(r.status == "accepted" for r in runs),
            },
            "settings": dict(self.queue.settings),
            "last_transition": last,
            # A boolean, not the tick time: unchanged state keeps its ETag between polls.
            "worker_alive": self.queue.alive,
            "error": self.queue.error,
            # Step shapes for the board; prompts are read with a definition or a run.
            "definitions": [
                {"digest": key, "workflow": outline(flow)} for key, flow in definitions
            ],
            "profile_config": self.agents.document(),
            "profiles": [encode(item) for item in self.agents.handlers.manifests()],
            "intents": self.flows.readiness(),
            "cooldowns": self.agents.resting(),
            "active_processes": len(self.queue.coordinator.active()),
            "projects": self.catalog.projects(),
            "role_defaults": ROLE_DEFAULTS,
            "task_metadata": self.catalog.task_metadata(),
            "plans": self.catalog.plans(),
            "locations": locations,
            "agent_discovery": self.catalog.discovery,
            "usage": self.catalog.usage(self.agents.profile_models()),
        }

    def definition(self, digest: str) -> dict[str, object]:
        """One published workflow in full, prompts included (the editor opens it)."""
        return encode(self.engine.store.workflow(digest))

    def detail(self, identifier: str) -> dict[str, object]:
        run = self.engine.store.get(identifier)
        with self.engine.store.unit() as unit:
            context = unit.context(identifier)
            results = unit.recent_results(identifier, 10)
        return {
            "run": encode(run),
            "context": context,
            "workflow": encode(self.engine.store.workflow(run.workflow_digest)),
            "events": self.engine.store.history(identifier, limit=1000),
            "results": [object_json(result) for result in results],
            "metadata": self.catalog.task_metadata().get(identifier, {}),
            "questions": self._questions(identifier),
            "tickets": self.documents.preview(identifier),
            "changes": self.reviews.proposal(identifier),
            "documents": self.documents.documents(identifier),
            "lane": self._lane(identifier),
        }

    def flight(self, run: str = "", level: str = "", limit: int = 300) -> list[dict[str, Json]]:
        return self.log.read(limit=limit, run=run, level=level)

    def live(self, identifier: str) -> dict[str, object]:
        run = self.engine.store.get(identifier)
        hosted = run.active is not None and run.active.id in self.queue.coordinator.active()
        return live(self.engine, identifier, hosted)

    def incident(self, identifier: str) -> dict[str, object]:
        return record(self.engine, self.log, identifier).document()

    def _lane(self, identifier: str) -> dict[str, object] | None:
        with self.engine.store.unit() as unit:
            document = unit.lane(identifier)
        return None if document is None else dict(object_json(document))

    def _questions(self, identifier: str) -> list[dict[str, object]]:
        try:
            return [encode(q) for q in questions(self.engine.asked(identifier))]
        except ValueError:
            return []
