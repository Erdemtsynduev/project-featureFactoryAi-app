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

from sdd_core.codec import canonical, flag, integer, object_json
from sdd_core.machine import WAIT_RETRY_LIMIT
from sdd_core.models import Json, Run, Workflow
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry, handler_key
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write

from sdd_ui.flightlog import FlightLog

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


class QueueController:
    def __init__(
        self,
        engine: Engine,
        handlers: Registry,
        settings_path: Path,
        health_path: Path,
        log: FlightLog,
        available: Callable[[str], bool],
    ) -> None:
        """`available(profile)` says whether a profile or its rotation can work now."""
        # Guards the coordinator (its live hosts and handlers) and the queue settings.
        self.lock = threading.RLock()
        self.engine, self.handlers = engine, handlers
        self.settings_path, self.health_path = settings_path, health_path
        self.log, self.available = log, available
        stored = (
            object_json(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        )
        self.settings: dict[str, Json] = {**DEFAULTS, **stored, "running": False}
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
        atomic_write(self.settings_path, canonical(self.settings))
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
        return bool(self.coordinator.live)

    @property
    def alive(self) -> bool:
        return self.last_tick is not None and time.time() - self.last_tick < 5

    def shutdown(self) -> None:
        with self.lock:
            if self.coordinator.live:
                raise Conflict("Pause the queue and wait for active executions before closing")
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
            for identifier in tuple(self.coordinator.live):
                run_id = self.coordinator.live[identifier].packet.run_id
                try:
                    self.coordinator.collect(identifier, now)
                except Exception as error:
                    self.coordinator.contain(run_id, now, error)
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
            or run.reason != WAIT_RETRY_LIMIT
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
