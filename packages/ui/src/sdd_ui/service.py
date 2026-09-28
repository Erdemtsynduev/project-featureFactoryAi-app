"""Local UI application adapter. All mutations use the existing engine contracts."""

import queue
import sys
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from sdd_core.codec import (
    canonical,
    flag,
    integer,
    mapping,
    object_json,
    sequence,
    text,
    workflow_load,
)
from sdd_core.graph import validate
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.profiles import resolve_profiles
from sdd_core.questions import questions
from sdd_core.sdk import handler_key
from sdd_providers.catalog import adapters
from sdd_runtime.composition import local_engine, registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.discovery import discover, list_models
from sdd_runtime.files import atomic_write, revision
from sdd_runtime.profiles import load_profiles
from sdd_runtime.rotation import Cooldowns
from sdd_workflows.templates import (
    CheckCommand,
    command_demo,
    feature,
    interview,
    localized,
    main_flow,
    question_example,
    ticket,
    with_checks,
)

from sdd_ui.subscriptions import read_codex_limits
from sdd_ui.workspace import WorkspaceCatalog

type Action = Callable[[dict[str, Json]], object]

# Actions that replace the coordinator, its handlers or the queue settings.
COORDINATOR_ACTIONS = frozenset({"queue", "budget", "profiles", "connect", "rotation"})


class WorkspaceService:
    def __init__(self, database: Path, config: Path | None = None) -> None:
        # Guards the coordinator (its live hosts and handlers) and the queue settings.
        # Reads and engine commands never take it: they use short transactions and CAS.
        self.lock = threading.RLock()
        self.engine = local_engine(database)
        store = self.engine.store
        self.catalog = WorkspaceCatalog(store.catalog(), store.path, store.workflow)
        self.health_path = database.with_suffix(".health.json")
        self.settings_path = database.resolve().with_suffix(".ui.json")
        self.config = config or database.resolve().with_suffix(".profiles.json")
        self.settings = (
            object_json(self.settings_path.read_text(encoding="utf-8"))
            if self.settings_path.exists()
            else {"running": False, "max_calls": 40, "max_planning_calls": 8}
        )
        self.settings["running"] = False
        self.handlers = registry(self.config if self.config.exists() else None)
        self.coordinator = Coordinator(self.engine, self.handlers, self.health_path)
        self.error: str | None = None
        self.last_tick: float | None = None
        self.quit = threading.Event()
        self.apply_limits()
        self.actions: dict[str, Action] = {
            "interactive-demo": self._interactive_demo,
            "project": self.catalog.save_project,
            "discover": lambda _: self.catalog.discover(),
            "subscription": self._subscription,
            "message": self._message,
            "recover": self._recover,
            "template": self._template,
            "validate": lambda doc: self._publish(doc, False),
            "publish": lambda doc: self._publish(doc, True),
            "create": self._create,
            "answer": self._answer,
            "queue": self._queue,
            "budget": self._budget,
            "profiles": lambda doc: self.install_profiles(doc["config"]),
            "connect": self.connect,
            "rotation": self.rotation,
            "models": self._models,
        }
        for command in ("pause", "resume", "retry", "stop", "auto", "manual"):
            self.actions[command] = partial(self._command, command)

    def apply_limits(self) -> None:
        maximum = integer(self.settings["max_calls"], "max_calls")
        planning = integer(self.settings["max_planning_calls"], "max_planning_calls")
        if not 0 <= planning <= maximum <= 100000:
            raise ValueError("Queue budgets must satisfy 0 <= planning <= total <= 100000")
        self.engine.max_queue_calls = maximum
        self.engine.max_queue_planning_calls = planning

    def state(self) -> dict[str, object]:
        with self.engine.store.unit() as unit:
            runs = unit.runs()
            last = unit.last_transition()
            locations = {run.id: unit.location(run.id)[0] for run in runs}
        return {
            "runs": [asdict(run) for run in reversed(runs)],
            "totals": {
                "calls": sum(r.calls for r in runs),
                "planning_calls": sum(r.planning_calls for r in runs),
                "tokens": sum(r.tokens for r in runs),
                "usage_unknown": any(r.usage_unknown for r in runs),
                "accepted": sum(r.status == "accepted" for r in runs),
            },
            "settings": dict(self.settings),
            "last_transition": last,
            # A boolean, not the tick time: unchanged state keeps its ETag between polls.
            "worker_alive": self.last_tick is not None and time.time() - self.last_tick < 5,
            "error": self.error,
            "definitions": [
                {"digest": key, "workflow": asdict(flow)}
                for key, flow in self.engine.store.definitions()
            ],
            "profile_config": object_json(self.config.read_text(encoding="utf-8"))
            if self.config.exists()
            else {"schema": 1, "runners": {}, "profiles": {}},
            "profiles": [asdict(item) for item in self.handlers.manifests()],
            "cooldowns": Cooldowns(self.config.with_suffix(".cooldowns.json")).read(),
            "active_processes": len(self.coordinator.live),
            "projects": self.catalog.projects(),
            "task_metadata": self.catalog.task_metadata(),
            "plans": self.catalog.plans(),
            "locations": locations,
            "agent_discovery": self.catalog.discovery,
            "usage": self.catalog.usage(self._profile_models()),
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
            "lane": self._lane(identifier),
        }

    def _lane(self, identifier: str) -> dict[str, object] | None:
        with self.engine.store.unit() as unit:
            document = unit.lane(identifier)
        return None if document is None else dict(object_json(document))

    def _questions(self, identifier: str) -> list[dict[str, object]]:
        try:
            return [asdict(q) for q in questions(self.engine.asked(identifier))]
        except ValueError:
            return []

    def mutate(self, action: str, doc: dict[str, Json]) -> object:
        """Run one operator action.

        Engine commands rely on optimistic versions and run concurrently with the
        queue; only actions that touch the coordinator or its settings take its lock.
        """
        handler = self.actions.get(action)
        if handler is None:
            raise ValueError("Unknown action")
        if action in COORDINATOR_ACTIONS:
            with self.lock:
                return handler(doc)
        return handler(doc)

    def _interactive_demo(self, doc: dict[str, Json]) -> object:
        language = text(doc.get("language", "ru"), "language")
        identifier = "question-" + uuid.uuid4().hex[:8]
        root = self.engine.store.path.parent / "question-examples" / identifier
        root.mkdir(parents=True)
        run = self.engine.create(
            identifier,
            self.engine.store.publish(question_example(language)),
            root,
            "Interactive question example; no model calls or project edits.",
            revision(root),
            time.time(),
        )
        self.engine.command(run.id, "resume", uuid.uuid4().hex, run.version, time.time())
        self.catalog.save_task(
            run.id,
            {
                "title": "Пример вопроса команды" if language == "ru" else "Team question example",
                "language": language,
                "project": "",
            },
        )
        return asdict(self.engine.dispatch(run.id, time.time(), uuid.uuid4().hex))

    def _subscription(self, doc: dict[str, Json]) -> object:
        candidates: list[tuple[str, ...]] = []
        if self.config.exists():
            candidates = [
                (runner.executable, *runner.arguments)
                for runner in load_profiles(self.config).runners.values()
                if runner.adapter == "codex"
            ]
        if not candidates:
            probe = discover(adapters()["codex"])
            candidates = [p.argv for p in probe.candidates if p.error is None]
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) != 1:
            raise ValueError("Select exactly one Codex installation in agent profiles")
        try:
            self.catalog.subscription = read_codex_limits(candidates[0])
        except (OSError, ValueError, queue.Empty) as error:
            self.catalog.subscription = {
                "status": "unavailable",
                "windows": [],
                "checked_at": time.time(),
                "error": type(error).__name__,
            }
        return self.catalog.subscription

    def _message(self, doc: dict[str, Json]) -> object:
        return asdict(
            self.engine.message(
                text(doc.get("id"), "id"),
                text(doc.get("message"), "message"),
                text(doc.get("request_id"), "request_id"),
                integer(doc.get("version"), "version"),
                time.time(),
            )
        )

    def _recover(self, doc: dict[str, Json]) -> object:
        return asdict(
            self.engine.request_recovery(
                text(doc.get("id"), "id"), integer(doc.get("version"), "version"), time.time()
            )
        )

    def _template(self, doc: dict[str, Json]) -> object:
        name = text(doc.get("name"), "name")
        project = self.catalog.project(text(doc.get("project", ""), "project"))
        if name == "demo":
            flow = command_demo(sys.executable)
        elif name == "ticket":
            # One check gate; project checks replace its command below.
            flow = ticket(
                (CheckCommand(("",), title="checks"),),
                isolated=project.get("isolation", True) is not False,
                auto_resolve=project.get("auto_resolve", True) is not False,
            )
        else:
            flow = {"feature": feature, "main-flow": main_flow, "interview": interview}[name]()
        flow = localized(flow, text(doc.get("language", "ru"), "language"))
        if project.get("checks"):
            flow = with_checks(flow, [text(x, "check") for x in sequence(project["checks"])])
        return asdict(flow)

    def _publish(self, doc: dict[str, Json], publish: bool) -> object:
        flow = workflow_load(canonical(doc["workflow"]))
        if self.config.exists():
            flow = resolve_profiles(flow, load_profiles(self.config).profiles)
        validate(flow)
        # Require every executable step's handler before declaring it runnable.
        for step in flow.steps:
            if step.kind == "check" and step.handler == "command":
                argv = sequence(object_json(step.config).get("argv", []))
                if not argv or not Path(text(argv[0], "executable")).is_absolute():
                    raise ValueError(
                        "Configure an absolute check executable in the project or step"
                    )
            if step.kind in ("agent", "check", "operation"):
                if step.kind not in self.handlers.get(handler_key(step)).manifest.capabilities:
                    raise ValueError(f"Incompatible handler on {step.id}")
        return {
            "workflow": asdict(flow),
            "digest": self.engine.store.publish(flow) if publish else None,
        }

    def _create(self, doc: dict[str, Json]) -> object:
        project_id = text(doc.get("project", ""), "project")
        project = self.catalog.project(project_id)
        if project_id and not project:
            raise ValueError("Unknown project")
        workspace = Path(text(project.get("workspace", doc.get("workspace")), "workspace")).resolve(
            strict=True
        )
        if self.engine.store.path.is_relative_to(workspace):
            raise ValueError("Keep the UI database outside the task workspace")
        language = text(doc.get("language", project.get("language", "ru")), "language")
        if language not in ("ru", "en"):
            raise ValueError("Unsupported response language")
        context = (
            "Response language: "
            + ("Russian" if language == "ru" else "English")
            + ". Write user-facing questions, summaries and explanations in this language;"
            " keep protocol keys in English.\n" + text(doc.get("context", ""), "context")
        )
        run = self.engine.create(
            text(doc.get("id"), "id"),
            text(doc.get("definition"), "definition"),
            workspace,
            context,
            revision(workspace),
            time.time(),
            tuple(text(x, "dependency") for x in sequence(doc.get("dependencies", []))),
        )
        self.catalog.save_task(
            run.id,
            {
                "project": project_id,
                "language": language,
                "title": text(doc.get("title", run.id), "title"),
            },
        )
        self.coordinator.bind(run.id)
        return asdict(run)

    def _answer(self, doc: dict[str, Json]) -> object:
        choices = {
            key: text(value, "choice") for key, value in mapping(doc.get("choices", {})).items()
        }
        return asdict(
            self.engine.answer(
                text(doc.get("id"), "id"),
                text(doc.get("outcome"), "outcome"),
                text(doc.get("answer", ""), "answer"),
                choices,
                integer(doc.get("version"), "version"),
                time.time(),
            )
        )

    def _command(self, command: str, doc: dict[str, Json]) -> object:
        return asdict(
            self.engine.command(
                text(doc.get("id"), "id"),
                command,
                text(doc.get("request_id", uuid.uuid4().hex), "request_id"),
                integer(doc.get("version"), "version"),
                time.time(),
            )
        )

    def _queue(self, doc: dict[str, Json]) -> object:
        running = flag(doc.get("running"), "running")
        if running and self.error:
            self.coordinator.close()
            self.coordinator = Coordinator(self.engine, self.handlers, self.health_path)
            self.coordinator.restore(time.time())
            self.error = None
        self.settings["running"] = running
        atomic_write(self.settings_path, canonical(self.settings))
        return dict(self.settings)

    def _budget(self, doc: dict[str, Json]) -> object:
        previous = self.settings.copy()
        self.settings.update(
            max_calls=integer(doc.get("max_calls"), "max_calls"),
            max_planning_calls=integer(doc.get("max_planning_calls"), "max_planning_calls"),
        )
        try:
            self.apply_limits()
        except ValueError:
            self.settings = previous
            raise
        atomic_write(self.settings_path, canonical(self.settings))
        return dict(self.settings)

    def _models(self, doc: dict[str, Json]) -> object:
        return {"models": list(self.models(text(doc.get("adapter"), "adapter")))}

    def _profile_models(self) -> dict[str, str]:
        if not self.config.exists():
            return {}
        profiles = object_json(self.config.read_text(encoding="utf-8")).get("profiles", {})
        return {
            name: str(mapping(item).get("model", "")) for name, item in mapping(profiles).items()
        }

    def install_profiles(self, config: Json) -> list[dict[str, object]]:
        if self.coordinator.live:
            raise Conflict("Wait for active executions before changing profiles")
        candidate = self.config.with_suffix(".candidate.json")
        try:
            atomic_write(candidate, canonical(config))
            load_profiles(candidate)
            handlers = registry(candidate)
            atomic_write(self.config, candidate.read_text(encoding="utf-8"))
            self.handlers = handlers
            self.coordinator.registry = handlers
        finally:
            candidate.unlink(missing_ok=True)
        return [asdict(item) for item in self.handlers.manifests()]

    def connect(self, doc: dict[str, Json]) -> list[dict[str, object]]:
        """Register a discovered CLI as a runner plus a named profile.

        The profile name is what workflow steps reference as their handler, so
        connecting `claude` and `codex` makes the bundled templates runnable.
        """
        adapter = text(doc.get("adapter"), "adapter")
        argv = [text(x, "argv") for x in sequence(doc.get("argv", []))]
        name = text(doc.get("name", adapter), "name").strip() or adapter
        if not argv or not all(Path(x).is_absolute() for x in argv[:1]):
            raise ValueError("Connect a discovered installation with an absolute executable")
        known = {candidate.argv for candidate in self.catalog.probes.get(adapter, ())}
        if tuple(argv) not in known:
            raise ValueError("Run discovery first; only discovered installations can be connected")
        current = (
            object_json(self.config.read_text(encoding="utf-8")) if self.config.exists() else {}
        )
        if "profiles" not in current:
            current = {"schema": 1, "runners": {}, "profiles": {}}
        runners = dict(object_json(canonical(current["runners"])))
        profiles = dict(object_json(canonical(current["profiles"])))
        runners[adapter] = {
            "adapter": adapter,
            "executable": argv[0],
            "arguments": list[Json](argv[1:]),
        }
        profiles[name] = {
            "runner": adapter,
            "model": text(doc.get("model"), "model").strip(),
            "permissions": text(doc.get("permissions", "workspace-write"), "permissions"),
            "timeout_seconds": integer(doc.get("timeout_seconds", 3600), "timeout_seconds"),
        }
        return self.install_profiles({**current, "runners": runners, "profiles": profiles})

    def rotation(self, doc: dict[str, Json]) -> list[dict[str, object]]:
        """Set or clear the fallback chain of one profile."""
        name = text(doc.get("name"), "name")
        current = (
            object_json(self.config.read_text(encoding="utf-8")) if self.config.exists() else {}
        )
        if name not in mapping(current.get("profiles", {})):
            raise ValueError("Connect the profile before configuring its rotation")
        rules = dict(mapping(current.get("rotation", {})))
        fallbacks = [text(x, "fallback") for x in sequence(doc.get("fallbacks", []))]
        if fallbacks:
            rules[name] = {
                "fallbacks": list[Json](fallbacks),
                "on": list[Json]([text(x, "failure") for x in sequence(doc.get("on", []))])
                or ["usage_limit", "rate_limit", "authentication"],
                "cooldown_minutes": integer(doc.get("cooldown_minutes", 60), "cooldown_minutes"),
                "retry_seconds": integer(doc.get("retry_seconds", 20), "retry_seconds"),
            }
        else:
            rules.pop(name, None)
        return self.install_profiles({**current, "rotation": rules})

    def models(self, adapter_id: str) -> tuple[str, ...]:
        """Model identifiers the installed CLI reports, or its documented aliases."""
        adapter = adapters()[adapter_id]
        found = next((d for d in self.catalog.discovery if d["adapter"] == adapter_id), None)
        selected = found.get("selected") if found else None
        suggestions = list(adapter.suggested_models)
        if adapter_id == "codex":
            configured = Path.home() / ".codex" / "config.toml"
            if configured.is_file():
                model = tomllib.loads(configured.read_text(encoding="utf-8")).get("model")
                if isinstance(model, str) and model:
                    suggestions.insert(0, model)
        if adapter.model_arguments is not None and isinstance(selected, (list, tuple)):
            try:
                listed = list_models(
                    tuple(str(x) for x in selected), adapter.model_arguments, adapter.parse_models
                )
                suggestions.extend(m for m in listed if m not in ("auto", "default"))
            except (ValueError, OSError, UnicodeDecodeError):
                pass
        return tuple(dict.fromkeys(suggestions))

    def work(self) -> None:
        """Queue loop. Per-run failures are contained by the coordinator; an error
        here means state could not be recorded, so the queue stops for the operator."""
        try:
            with self.lock:
                try:
                    self.coordinator.restore(time.time())
                except Exception as error:
                    self.error = f"{type(error).__name__}: {error}"
                    self.settings["running"] = False
            while not self.quit.wait(0.3):
                with self.lock:
                    try:
                        if self.settings["running"] and not self.error:
                            self.coordinator.tick()
                        else:
                            now = time.time()
                            for identifier in tuple(self.coordinator.live):
                                run_id = self.coordinator.live[identifier].packet.run_id
                                try:
                                    self.coordinator.collect(identifier, now)
                                except Exception as error:
                                    self.coordinator.contain(run_id, now, error)
                        self.last_tick = time.time()
                    except Exception as error:
                        self.error = f"{type(error).__name__}: {error}"
                        self.settings["running"] = False
                        atomic_write(self.settings_path, canonical(self.settings))
        except Exception as error:
            with self.lock:
                self.error = f"{type(error).__name__}: {error}"
                self.settings["running"] = False
        finally:
            with self.lock:
                self.coordinator.close()

    def close(self) -> None:
        self.quit.set()
