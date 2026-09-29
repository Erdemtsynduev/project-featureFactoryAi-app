"""The queue: settings, the coordinator that owns processes, and its work loop.

Only this class starts, ticks, restores or replaces the coordinator. Queue
settings are persisted; `running` always starts false so a restart never
resumes work silently. Per-run failures are contained by the coordinator; an
exception here means state could not be recorded, so the queue stops.
"""

import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sdd_core.catalog import CatalogRecords
from sdd_core.codec import canonical, flag, integer, object_json
from sdd_core.models import Json, Run, Workflow
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry, handler_key
from sdd_factory.journal import FlightLog
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine

DEFAULTS: dict[str, Json] = {
    "running": False,
    "max_calls": 40,
    "max_planning_calls": 8,
    "revive": True,
}
# A run blocked after repeated limit waits is retried once its agents rest no more.
REVIVE_AFTER = 30 * 60
MAX_REVIVALS = 5
WATCH_EVERY = 1.0


class QueueSettings:
    """Queue settings kept in the control database with everything else.

    An earlier release kept them in `<database>.ui.json`; that file is adopted once
    and renamed, so there is one source of truth.
    """

    KEY = "queue-settings"

    def __init__(self, records: CatalogRecords, earlier: Path) -> None:
        self.records, self.earlier = records, earlier

    def load(self) -> dict[str, Json]:
        raw = self.records.preference(self.KEY)
        if raw is None and self.earlier.is_file():
            raw = canonical(object_json(self.earlier.read_text(encoding="utf-8")))
            self.records.save_preference(self.KEY, raw)
            self.earlier.replace(self.earlier.with_suffix(".json.adopted"))
        return object_json(raw) if raw else {}

    def save(self, settings: dict[str, Json]) -> None:
        self.records.save_preference(self.KEY, canonical(settings))


class QueueController:
    def __init__(
        self,
        engine: Engine,
        handlers: Registry,
        store: QueueSettings,
        health_path: Path,
        log: FlightLog,
        available: Callable[[str], bool],
    ) -> None:
        """`available(profile)` says whether a profile or its rotation can work now."""
        # Guards the coordinator (its live hosts and handlers) and the queue settings.
        self.lock = threading.RLock()
        self.engine, self.handlers = engine, handlers
        self.store, self.health_path = store, health_path
        self.log, self.available = log, available
        stored = store.load()
        # Opening the application never starts work, except right after a restart
        # the operator asked for: then the queue continues as it was.
        resumed = bool(stored.pop("restarting", False)) and bool(stored.get("running"))
        self.settings: dict[str, Json] = {**DEFAULTS, **stored, "running": resumed}
        self.coordinator = Coordinator(engine, handlers, health_path)
        self.error: str | None = None
        self.last_tick: float | None = None
        self.quit = threading.Event()
        self.revivals: dict[str, int] = {}
        self.statuses: dict[str, tuple[str, str, bool]] = {}
        self.workflows: dict[str, Workflow] = {}
        self.watched = 0.0
        self.apply_limits()

    # Settings -----------------------------------------------------------------

    def apply_limits(self) -> None:
        maximum = integer(self.settings["max_calls"], "max_calls")
        planning = integer(self.settings["max_planning_calls"], "max_planning_calls")
        if not 0 <= planning <= maximum <= 100000:
            raise ValueError("Queue budgets must satisfy 0 <= planning <= total <= 100000")
        self.engine.max_queue_calls = maximum
        self.engine.max_queue_planning_calls = planning

    def save(self) -> dict[str, Json]:
        self.store.save(self.settings)
        return dict(self.settings)

    def set_running(self, doc: dict[str, Json]) -> dict[str, Json]:
        running = flag(doc.get("running"), "running")
        if running and self.error:
            self.coordinator.close()
            self.coordinator = Coordinator(self.engine, self.handlers, self.health_path)
            self.coordinator.restore(time.time())
            self.log.record("queue_restarted", previous_error=self.error)
            self.error = None
        self.settings["running"] = running
        self.log.record("queue_started" if running else "queue_paused")
        return self.save()

    def set_budget(self, doc: dict[str, Json]) -> dict[str, Json]:
        previous = dict(self.settings)
        self.settings.update(
            max_calls=integer(doc.get("max_calls"), "max_calls"),
            max_planning_calls=integer(doc.get("max_planning_calls"), "max_planning_calls"),
        )
        if "revive" in doc:
            self.settings["revive"] = flag(doc.get("revive"), "revive")
        try:
            self.apply_limits()
        except ValueError:
            self.settings = previous
            raise
        self.log.record("budget_changed", settings=dict(self.settings))
        return self.save()

    def use(self, handlers: Registry) -> None:
        self.handlers = handlers
        self.coordinator.registry = handlers

    @property
    def busy(self) -> bool:
        return bool(self.coordinator.active())

    @property
    def alive(self) -> bool:
        return self.last_tick is not None and time.time() - self.last_tick < 5

    def shutdown(self, keep_running: bool = False) -> None:
        with self.lock:
            if self.coordinator.active():
                raise Conflict("Pause the queue and wait for active executions before closing")
            if keep_running:
                self.store.save({**self.settings, "restarting": True})
            else:
                self.settings["running"] = False
                self.save()

    # Work loop ----------------------------------------------------------------

    def work(self) -> None:
        try:
            with self.lock:
                try:
                    self.coordinator.restore(time.time())
                except Exception as error:
                    self._stop(error)
            while not self.quit.wait(0.3):
                with self.lock:
                    try:
                        self._pass(time.time())
                        self.last_tick = time.time()
                    except Exception as error:
                        self._stop(error)
                        self.save()
        except Exception as error:
            with self.lock:
                self._stop(error)
        finally:
            with self.lock:
                self.coordinator.close()

    def close(self) -> None:
        self.quit.set()

    def _stop(self, error: Exception) -> None:
        self.error = f"{type(error).__name__}: {error}"
        self.settings["running"] = False
        self.log.record("queue_stopped", "error", error=self.error)

    def _pass(self, now: float) -> None:
        if self.settings["running"] and not self.error:
            dispatched = self.coordinator.tick(now)
            if dispatched:
                self.log.record("dispatched", count=dispatched)
        else:
            # Paused queue still collects finished work, so nothing is left dangling.
            self.coordinator.collect(now)
        if now - self.watched >= WATCH_EVERY:
            self.watched = now
            self._watch(now)

    def _watch(self, now: float) -> None:
        """Log changes worth a notification or a post-mortem; revive runs after limits."""
        with self.engine.store.unit() as db:
            runs = db.runs()
        # The first pass is a baseline: restarting announces nothing twice.
        baseline = not self.statuses
        for run in runs:
            awaiting = run.active is not None and self._kind(run) == "human"
            seen = (run.status, run.reason, awaiting)
            before = self.statuses.get(run.id, ("", "", False))
            self.statuses[run.id] = seen
            if not baseline and before != seen:
                self._announce(run, awaiting, before[2])
            if self.settings.get("revive") and self.settings["running"]:
                self._revive(run, now)

    def _kind(self, run: Run) -> str:
        if run.workflow_digest not in self.workflows:
            self.workflows[run.workflow_digest] = self.engine.store.workflow(run.workflow_digest)
        return self.workflows[run.workflow_digest].step(run.step).kind

    def _announce(self, run: Run, awaiting: bool, was_awaiting: bool) -> None:
        if awaiting and not was_awaiting:
            self.log.record("question", "warning", run=run.id, step=run.step)
        elif run.status == "accepted":
            self.log.record("accepted", run=run.id)
        elif run.status in ("blocked", "waiting"):
            level = "warning" if run.status == "waiting" else "error"
            self.log.record(run.status, level, run=run.id, step=run.step, reason=run.reason)

    def _revive(self, run: Run, now: float) -> None:
        if (
            run.status != "blocked"
            or run.cause != "wait_limit"
            or run.active is not None
            or self.revivals.get(run.id, 0) >= MAX_REVIVALS
        ):
            return
        history = self.engine.store.history(run.id, limit=1000)
        since = float(str(history[-1]["at"])) if history else now
        if now - since < REVIVE_AFTER:
            return
        step = self.engine.store.workflow(run.workflow_digest).step(run.step)
        if step.kind == "agent" and not self.available(handler_key(step)):
            return
        try:
            self.engine.command(run.id, "retry", uuid.uuid4().hex, run.version, now)
        except (ValueError, Conflict):
            return
        self.revivals[run.id] = self.revivals.get(run.id, 0) + 1
        self.log.record("revived", "warning", run=run.id, step=run.step)
