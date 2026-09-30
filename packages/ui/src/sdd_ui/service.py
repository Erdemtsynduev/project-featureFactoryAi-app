"""Composition root of the local UI: wires the use-case services and routes actions.

Each concern lives in its own module: `queue` (coordinator and settings),
`agents` (profiles, rotation, quotas), `flows` (templates and publication),
`tasks` (task use cases). This class only composes them, answers read models
and maps HTTP action names to use cases. Every action is recorded in the
flight log with its outcome, so incidents can be reconstructed afterwards.
"""

import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

from sdd_core.codec import encode, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry
from sdd_factory.admission import TicketAdmission
from sdd_factory.answers import Answers
from sdd_factory.control import WorkControl
from sdd_factory.documents import FeatureDocuments
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
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
from sdd_ui.background import Periodic, start
from sdd_ui.board_view import BoardView
from sdd_ui.queue import QueueController, QueueSettings
from sdd_ui.workspace import WorkspaceCatalog

type Action = Callable[[dict[str, Json]], object]

# Actions that replace the coordinator, its handlers or the queue settings.
# Blocks earlier engine builds recorded where nothing had failed; each is released once,
# as the operator's Retry would be, with the reason logged.
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
            QueueSettings(store.catalog()),
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
        self.board = BoardView(self)
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
        start(
            (
                # Mirror projects into trackers outside the queue's lock: delivery waits
                # on the network and must never hold up scheduling.
                Periodic("tracker-mirror", TRACKER_EVERY, self.sync_trackers, "tracker_failed"),
                # Subscription windows are read on their own schedule: a spent
                # subscription rests its profiles until the window resets.
                Periodic(
                    "subscription-quotas",
                    QUOTA_EVERY,
                    lambda: self.agents.watch_quotas(time.time()),
                    "quota_failed",
                    "warning",
                    at_start=True,
                ),
                # Reviews a busy plan postponed: one review runs per plan at a time, so a
                # ticket that stopped meanwhile is reviewed once the plan's review ends.
                Periodic("plan-reviews", REVIEW_EVERY, self.reviews.sweep, "plan_review_failed"),
            ),
            self.queue.quit,
            self.log,
        )
        self.queue.work()

    # Trackers ------------------------------------------------------------------

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
            for r in cast(list[dict[str, Any]], self.board.runs())
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
        return self.board.state()

    def definition(self, digest: str) -> dict[str, object]:
        return self.board.definition(digest)

    def detail(self, identifier: str) -> dict[str, object]:
        return self.board.detail(identifier)

    def flight(self, run: str = "", level: str = "", limit: int = 300) -> list[dict[str, Json]]:
        return self.board.flight(run, level, limit)

    def live(self, identifier: str) -> dict[str, object]:
        return self.board.live(identifier)

    def incident(self, identifier: str) -> dict[str, object]:
        return self.board.incident(identifier)
