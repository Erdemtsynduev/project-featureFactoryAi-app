"""Composition root of the local UI: wires the use-case services and routes actions.

Each concern lives in its own module: `queue` (coordinator and settings),
`agents` (profiles, rotation, quotas), `flows` (templates and publication),
`tasks` (task use cases). This class only composes them, answers read models
and maps HTTP action names to use cases. Every action is recorded in the
flight log with its outcome, so incidents can be reconstructed afterwards.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import cache, partial
from pathlib import Path

from sdd_core.codec import object_json, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.questions import questions
from sdd_core.sdk import Registry
from sdd_factory.diagnostics import live, record
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.plans import PlanService
from sdd_factory.tasks import TaskService
from sdd_runtime.composition import local_engine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.versions import consistent, engine, installed

from sdd_ui.agents import AgentSettings
from sdd_ui.attention import attention, calls_needed, lane, outline, projection
from sdd_ui.queue import QueueController, QueueSettings
from sdd_ui.workspace import WorkspaceCatalog

type Action = Callable[[dict[str, Json]], object]

# Actions that replace the coordinator, its handlers or the queue settings.
SPURIOUS_BLOCK = "Run is not dispatchable"
COORDINATOR_ACTIONS = frozenset({"queue", "budget", "profiles", "connect", "rotation", "wake"})
# Actions whose `id` names a task (other actions use `id` for projects).
TASK_ACTIONS = frozenset(
    {
        "create",
        "rename",
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
        )
        self.lock = self.queue.lock
        self.flows = FlowLibrary(
            self.engine, self.config, lambda: self.agents.handlers, self.catalog.project
        )
        self.tasks = TaskService(
            self.engine, self.catalog, self.flows, self.log, lambda run: self.coordinator.bind(run)
        )
        self.plans = PlanService(self.engine, self.catalog, self.flows, self.log)
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
            "create": self.tasks.create,
            "rename": self.tasks.rename,
            "plans-sync": self.plans.sync,
            "plans-rebuild": self.plans.rebuild,
            "interactive-demo": self.tasks.demo,
            "answer": self.tasks.answer,
            "message": self.tasks.message,
            "recover": self.tasks.recover,
        }
        for command in ("pause", "resume", "retry", "stop", "auto", "manual"):
            self.actions[command] = partial(self.tasks.command, command)
        for command in ("resume", "pause"):
            self.actions[command + "-many"] = partial(self.tasks.bulk, command)
        found = installed()
        self.versions = {"engine": engine(), "packages": found, "consistent": consistent(found)}
        self._release_spurious_blocks()

    def _release_spurious_blocks(self) -> None:
        """Earlier builds blocked a task paused during its own dispatch with this
        reason. Nothing failed, so it is released exactly once, as the operator's
        Retry would, and logged; it stays paused."""
        with self.engine.store.unit() as unit:
            stuck = [
                run
                for run in unit.runs()
                if run.status == "blocked" and run.active is None and run.reason == SPURIOUS_BLOCK
            ]
        for run in stuck:
            try:
                self.engine.command(run.id, "retry", uuid.uuid4().hex, run.version, time.time())
            except (ValueError, Conflict):
                continue
            self.log.record("unblocked", run=run.id, reason=run.reason)

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
        return asdict(flow)

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
        self.queue.work()

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
        reasons = {
            run.id: asdict(
                attention(
                    run,
                    workflows[run.workflow_digest].step(run.step),
                    queue_running=running,
                    pending=pending.get(run.id, ()),
                    has_profile=profiles.__contains__,
                    available=available,
                )
            )
            for run in runs
        }
        return {
            "runs": [
                {
                    **projection(run),
                    "attention": reasons[run.id],
                    "lane": lane(str(reasons[run.id]["tone"])),
                    "needs": calls_needed(run, workflows[run.workflow_digest]),
                    "pending_dependencies": list(pending.get(run.id, ())),
                    "dependencies": prerequisites.get(run.id, []),
                }
                for run in reversed(runs)
            ],
            "versions": self.versions,
            "totals": {
                "calls": sum(r.calls for r in runs),
                "planning_calls": sum(r.planning_calls for r in runs),
                "tokens": sum(r.tokens for r in runs),
                "usage_unknown": any(r.usage_unknown for r in runs),
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
            "profiles": [asdict(item) for item in self.agents.handlers.manifests()],
            "intents": self.flows.readiness(),
            "cooldowns": self.agents.resting(),
            "active_processes": len(self.queue.coordinator.live),
            "projects": self.catalog.projects(),
            "task_metadata": self.catalog.task_metadata(),
            "plans": self.catalog.plans(),
            "locations": locations,
            "agent_discovery": self.catalog.discovery,
            "usage": self.catalog.usage(self.agents.profile_models()),
        }

    def definition(self, digest: str) -> dict[str, object]:
        """One published workflow in full, prompts included (the editor opens it)."""
        return asdict(self.engine.store.workflow(digest))

    def detail(self, identifier: str) -> dict[str, object]:
        run = self.engine.store.get(identifier)
        with self.engine.store.unit() as unit:
            context = unit.context(identifier)
            results = unit.recent_results(identifier, 10)
        return {
            "run": asdict(run),
            "context": context,
            "workflow": asdict(self.engine.store.workflow(run.workflow_digest)),
            "events": self.engine.store.history(identifier, limit=1000),
            "results": [object_json(result) for result in results],
            "metadata": self.catalog.task_metadata().get(identifier, {}),
            "questions": self._questions(identifier),
            "tickets": self.tasks.preview(identifier),
            "documents": self.tasks.documents(identifier),
            "lane": self._lane(identifier),
        }

    def flight(self, run: str = "", level: str = "", limit: int = 300) -> list[dict[str, Json]]:
        return self.log.read(limit=limit, run=run, level=level)

    def live(self, identifier: str) -> dict[str, object]:
        run = self.engine.store.get(identifier)
        hosted = run.active is not None and run.active.id in self.queue.coordinator.live
        return live(self.engine, identifier, hosted)

    def incident(self, identifier: str) -> dict[str, object]:
        return record(self.engine, self.log, identifier).document()

    def _lane(self, identifier: str) -> dict[str, object] | None:
        with self.engine.store.unit() as unit:
            document = unit.lane(identifier)
        return None if document is None else dict(object_json(document))

    def _questions(self, identifier: str) -> list[dict[str, object]]:
        try:
            return [asdict(q) for q in questions(self.engine.asked(identifier))]
        except ValueError:
            return []
