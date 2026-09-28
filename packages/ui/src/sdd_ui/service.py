"""Composition root of the local UI: wires the use-case services and routes actions.

Each concern lives in its own module: `queue` (coordinator and settings),
`agents` (profiles, rotation, quotas), `flows` (templates and publication),
`tasks` (task use cases). This class only composes them, answers read models
and maps HTTP action names to use cases. Every action is recorded in the
flight log with its outcome, so incidents can be reconstructed afterwards.
"""

import time
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from sdd_core.codec import object_json, text
from sdd_core.models import Json
from sdd_core.questions import questions
from sdd_core.sdk import Registry
from sdd_runtime.composition import local_engine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.versions import consistent, engine, installed

from sdd_ui.agents import AgentSettings
from sdd_ui.attention import attention
from sdd_ui.diagnostics import live, record
from sdd_ui.flightlog import FlightLog
from sdd_ui.flows import FlowLibrary
from sdd_ui.queue import QueueController
from sdd_ui.tasks import TaskService
from sdd_ui.workspace import WorkspaceCatalog

type Action = Callable[[dict[str, Json]], object]

# Actions that replace the coordinator, its handlers or the queue settings.
COORDINATOR_ACTIONS = frozenset({"queue", "budget", "profiles", "connect", "rotation", "wake"})
# Actions whose `id` names a task (other actions use `id` for projects).
TASK_ACTIONS = frozenset(
    {"create", "answer", "message", "recover", "pause", "resume", "retry", "stop", "auto", "manual"}
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
            database.resolve().with_suffix(".ui.json"),
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

    def shutdown(self) -> None:
        self.queue.shutdown()
        self.log.record("application_closed")

    def work(self) -> None:
        self.log.record("application_started")
        self.queue.work()

    def close(self) -> None:
        self.queue.close()

    # Read models ---------------------------------------------------------------

    def state(self) -> dict[str, object]:
        with self.engine.store.unit() as unit:
            runs = unit.runs()
            last = unit.last_transition()
            locations = {run.id: unit.location(run.id)[0] for run in runs}
            pending = {
                run.id: tuple(d.id for d in unit.dependencies(run.id) if d.status != "accepted")
                for run in runs
                if run.status != "accepted"
            }
        definitions = self.engine.store.definitions()
        workflows = dict(definitions)
        profiles = {manifest.id for manifest in self.agents.handlers.manifests()}
        running = bool(self.queue.settings["running"]) and not self.queue.error
        reasons = {
            run.id: asdict(
                attention(
                    run,
                    workflows[run.workflow_digest].step(run.step),
                    queue_running=running,
                    pending=pending.get(run.id, ()),
                    has_profile=profiles.__contains__,
                    available=self.agents.available,
                )
            )
            for run in runs
        }
        return {
            "runs": [
                {
                    **asdict(run),
                    "attention": reasons[run.id],
                    "pending_dependencies": list(pending.get(run.id, ())),
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
            "definitions": [{"digest": key, "workflow": asdict(flow)} for key, flow in definitions],
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
