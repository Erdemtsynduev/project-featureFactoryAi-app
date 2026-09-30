"""Composition root of the local UI: wires the use-case services and routes actions.

Each concern lives in its own module: `queue` (coordinator and settings),
`agents` (profiles, rotation, quotas), `flows` (templates and publication),
`tasks` (task use cases). This class only composes them, answers read models
and maps HTTP action names to use cases. Every action is recorded in the
flight log with its outcome, so incidents can be reconstructed afterwards.
"""

import threading
import time
import uuid
from collections.abc import Callable
from functools import cache, partial
from pathlib import Path
from typing import Any, cast

from sdd_core.codec import encode, object_json, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.questions import questions
from sdd_core.sdk import Registry
from sdd_factory.admission import TicketAdmission
from sdd_factory.answers import Answers
from sdd_factory.control import WorkControl
from sdd_factory.diagnostics import live, record
from sdd_factory.documents import FeatureDocuments
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import ticket_places
from sdd_factory.plans import PlanService
from sdd_factory.reflow import FlowChanges
from sdd_factory.reviews import PlanReviews
from sdd_factory.supersession import Supersession
from sdd_factory.trackers import ProjectSources, installed_trackers
from sdd_factory.tracking import RunView, TrackerSync
from sdd_factory.work import WorkCreation
from sdd_runtime.composition import local_engine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.versions import consistent, engine, installed

from sdd_ui.agents import AgentSettings
from sdd_ui.attention import attention, calls_needed, lane, outline, projection, rollup
from sdd_ui.queue import QueueController, QueueSettings
from sdd_ui.workspace import WorkspaceCatalog

type Action = Callable[[dict[str, Json]], object]

# Actions that replace the coordinator, its handlers or the queue settings.
# Blocks earlier engine builds recorded where nothing had failed; each is released once,
# as the operator's Retry would be, with the reason logged.
LEGACY_BLOCKS = {
    "Run is not dispatchable": "paused during its own dispatch",
    "Pinned handler settings or version changed": "an upgraded agent or profile pinned per run",
}
# How often projects with a tracker are mirrored, in seconds.
TRACKER_EVERY = 30.0
# How often the quota loop wakes; each subscription is read on its own slower schedule.
QUOTA_EVERY = 60.0
# How often stopped tickets are checked for a plan review a busy plan postponed.
REVIEW_EVERY = 60.0
COORDINATOR_ACTIONS = frozenset({"queue", "budget", "profiles", "connect", "rotation", "wake"})
# Actions whose `id` names a task (other actions use `id` for projects).
TASK_ACTIONS = frozenset(
    {
        "create",
        "rename",
        "close",
        "flow-change",
        "flow-update",
        "answer",
        "message",
        "recover",
        "pause",
        "resume",
        "retry",
        "stop",
        "auto",
        "manual",
    }
)
# Probes that are logged only when they fail; they change no state.
QUIET_ACTIONS = frozenset({"models", "template", "validate"})


class WorkspaceService:
    def __init__(self, database: Path, config: Path | None = None) -> None:
        self.engine = local_engine(database)
        store = self.engine.store
        self.catalog = WorkspaceCatalog(store.catalog(), store.path, store.workflow)
        self.log = FlightLog(database.resolve().with_suffix(".flight.jsonl"))
        self.config = config or database.resolve().with_suffix(".profiles.json")
        self.agents = AgentSettings(
            self.config, self.catalog, lambda: self.queue.busy, self._installed
        )
        self.queue = QueueController(
            self.engine,
            self.agents.handlers,
            QueueSettings(store.catalog(), database.resolve().with_suffix(".ui.json")),
            database.with_suffix(".health.json"),
            self.log,
            self.agents.available,
            lambda run: self.reviews.blocked(run),
        )
        self.lock = self.queue.lock
        self.flows = FlowLibrary(
            self.engine, self.config, lambda: self.agents.handlers, self.catalog.project
        )
        self.creation = WorkCreation(self.engine, self.catalog, self.flows, self.log, self._bind)
        self.control = WorkControl(self.engine, self.catalog, self.log)
        self.supersession = Supersession(self.engine, self.catalog, self.log)
        self.documents = FeatureDocuments(self.engine, self.catalog)
        self.admission = TicketAdmission(
            self.engine, self.catalog, self.flows, self.log, self._bind, self.documents
        )
        # A plan lead reviews approved plans; its approved proposals change the tickets.
        self.reviews = PlanReviews(self.engine, self.catalog, self.flows, self.admission, self.log)
        self.answers = Answers(self.catalog, self.admission, self.reviews)
        # A project's work comes from its plans folder or its tracker, which also
        # receives the factory's progress through the outbox.
        self.sources = ProjectSources()
        self.plans = PlanService(
            self.engine, self.catalog, self.flows, self.log, self.sources.source
        )
        self.mirror = TrackerSync(self.catalog, self.sources.tracker, self.log)
        self.reflow = FlowChanges(self.engine, self.catalog, self.flows, self.log)
        self.actions: dict[str, Action] = {
            "project": self.catalog.save_project,
            "discover": lambda _: self.catalog.discover(),
            "subscription": lambda _: self.agents.subscription(),
            "models": lambda doc: {
                "models": list(self.agents.models(text(doc["adapter"], "adapter")))
            },
            "profiles": lambda doc: self.agents.install(doc["config"]),
            "connect": self.agents.connect,
            "rotation": self.agents.rotation,
            "wake": self.agents.wake,
            "template": self._template,
            "validate": lambda doc: self.flows.check(doc["workflow"], False),
            "publish": lambda doc: self.flows.check(doc["workflow"], True),
            "queue": self.queue.set_running,
            "budget": self.queue.set_budget,
            "create": self.creation.create,
            "rename": self.creation.rename,
            "close": self.supersession.close,
            "supersede": self.supersession.supersede,
            "plans-sync": self.plans.sync,
            "plans-rebuild": self.plans.rebuild,
            "flow-change": self.reflow.change,
            "flow-update": self.reflow.update,
            "flows-update": self.reflow.update_many,
            "interactive-demo": self.creation.demo,
            "tracker-sync": lambda doc: self.sync_trackers(text(doc.get("project", ""), "project")),
            "answer": self.answers.answer,
            "message": self.control.message,
            "recover": self.control.recover,
        }
        for command in ("pause", "resume", "retry", "stop", "auto", "manual"):
            self.actions[command] = partial(self.control.command, command)
        for command in ("resume", "pause"):
            self.actions[command + "-many"] = partial(self.control.bulk, command)
        found = installed()
        self.versions = {"engine": engine(), "packages": found, "consistent": consistent(found)}
        # Tracker adapters installed with the application, offered in project settings.
        self.tracker_kinds = sorted(installed_trackers())
        self._release_legacy_blocks()
        self._finish_admissions()

    def _bind(self, run_id: str) -> None:
        """Pin a new run's handlers; the coordinator exists by the time work is created."""
        self.coordinator.bind(run_id)

    def _finish_admissions(self) -> None:
        """Finish what a crash or restart cut short: tickets an approval left out,
        approved plan reviews half applied, and reviews for tickets blocked meanwhile."""
        try:
            created = self.admission.readmit()
            self.reviews.reapply()
            self.reviews.sweep()
        except (ValueError, KeyError, OSError, Conflict) as error:
            self.log.record("readmission_failed", "error", error=f"{type(error).__name__}: {error}")
            return
        if created:
            self.log.record("tickets_readmitted", tickets=list[Json](created))

    def _release_legacy_blocks(self) -> None:
        """Release blocks an earlier engine recorded where nothing failed (see
        LEGACY_BLOCKS), exactly once, as the operator's Retry would; logged."""
        with self.engine.store.unit() as unit:
            stuck = [
                run
                for run in unit.runs()
                if run.status == "blocked" and run.active is None and run.reason in LEGACY_BLOCKS
            ]
        for run in stuck:
            try:
                self.engine.command(run.id, "retry", uuid.uuid4().hex, run.version, time.time())
            except (ValueError, Conflict):
                continue
            self.log.record(
                "unblocked", run=run.id, reason=run.reason, legacy=LEGACY_BLOCKS[run.reason]
            )

    # Compatibility accessors used by the HTTP layer and tests.

    @property
    def coordinator(self) -> Coordinator:
        return self.queue.coordinator

    @property
    def settings(self) -> dict[str, Json]:
        return self.queue.settings

    @property
    def handlers(self) -> Registry:
        return self.agents.handlers

    def _installed(self, handlers: Registry) -> None:
        self.queue.use(handlers)

    def _template(self, doc: dict[str, Json]) -> object:
        flow = self.flows.template(
            text(doc.get("name"), "name"),
            text(doc.get("project", ""), "project"),
            text(doc.get("language", "ru"), "language"),
        )
        return encode(flow)

    # Actions -------------------------------------------------------------------

    def mutate(self, action: str, doc: dict[str, Json]) -> object:
        """Run one operator action and record how it ended.

        Engine commands rely on optimistic versions and run concurrently with the
        queue; only actions that touch the coordinator or its settings take its lock.
        """
        handler = self.actions.get(action)
        if handler is None:
            raise ValueError("Unknown action")
        run = doc.get("id") if action in TASK_ACTIONS and isinstance(doc.get("id"), str) else ""
        started = time.monotonic()
        try:
            if action in COORDINATOR_ACTIONS:
                with self.queue.lock:
                    result = handler(doc)
            else:
                result = handler(doc)
        except Exception as error:
            self.log.record(
                "action", "error", str(run), action=action, error=f"{type(error).__name__}: {error}"
            )
            raise
        if action not in QUIET_ACTIONS:
            elapsed = round(time.monotonic() - started, 3)
            self.log.record("action", run=str(run), action=action, seconds=elapsed)
        return result

    def shutdown(self, keep_queue: bool = False) -> None:
        """Stop serving; a restart keeps the queue's running state for the new process."""
        self.queue.shutdown(keep_queue)
        self.log.record("application_restarting" if keep_queue else "application_closed")

    def work(self) -> None:
        self.log.record("application_started")
        mirror = threading.Thread(target=self._mirror_loop, name="tracker-mirror", daemon=True)
        mirror.start()
        quotas = threading.Thread(target=self._quota_loop, name="subscription-quotas", daemon=True)
        quotas.start()
        reviews = threading.Thread(target=self._review_loop, name="plan-reviews", daemon=True)
        reviews.start()
        self.queue.work()

    def _review_loop(self) -> None:
        """Ask for reviews a busy plan postponed: one review runs per plan at a time, so a
        ticket that stopped meanwhile is reviewed once the plan's review has finished."""
        while not self.queue.quit.wait(REVIEW_EVERY):
            try:
                self.reviews.sweep()
            except Exception as error:  # a review request never stops the application
                self.log.record(
                    "plan_review_failed", "error", error=f"{type(error).__name__}: {error}"
                )

    def _quota_loop(self) -> None:
        """Read subscription windows on their own schedule, outside the queue's lock:
        a spent subscription rests its profiles until the window resets."""
        while True:
            try:
                self.agents.watch_quotas(time.time())
            except Exception as error:  # a quota probe never stops the application
                self.log.record("quota_failed", "warning", error=f"{type(error).__name__}: {error}")
            if self.queue.quit.wait(QUOTA_EVERY):
                return

    # Trackers ------------------------------------------------------------------

    def _mirror_loop(self) -> None:
        """Mirror projects into their trackers outside the queue's lock: delivery
        waits on the network and must never hold up scheduling."""
        while not self.queue.quit.wait(TRACKER_EVERY):
            try:
                self.sync_trackers()
            except Exception as error:  # a tracker never stops the application
                self.log.record("tracker_failed", "error", error=f"{type(error).__name__}: {error}")

    def sync_trackers(self, project: str = "") -> dict[str, int]:
        """Record what changed for projects with a tracker, then deliver in order."""
        projects = [
            p
            for p in self.catalog.projects()
            if p.get("tracker") and (not project or p["id"] == project)
        ]
        if not projects:
            return {"recorded": 0, "sent": 0}
        views = [
            RunView(
                str(r["id"]),
                str(r["status"]),
                str(r["attention"]["code"]),
                str(r["attention"].get("detail") or r.get("reason") or ""),
            )
            for r in cast(list[dict[str, Any]], self.state()["runs"])
        ]
        recorded = sent = 0
        for item in projects:
            recorded += self.mirror.observe(str(item["id"]), views)
            sent += self.mirror.deliver(item)
        return {"recorded": recorded, "sent": sent}

    def close(self) -> None:
        self.queue.close()

    # Read models ---------------------------------------------------------------

    def state(self) -> dict[str, object]:
        """The board's read model: one projection per run with what the views show,
        derived on the server (lane, attention, waits, calls still needed). The task
        drawer reads a run in full through `detail`."""
        with self.engine.store.unit() as unit:
            runs = unit.runs()
            last = unit.last_transition()
            locations = dict(unit.locations())
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
        return {
            "runs": [
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
            ],
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
