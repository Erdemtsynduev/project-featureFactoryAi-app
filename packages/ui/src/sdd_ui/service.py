"""Local UI application adapter. All mutations use the existing engine contracts."""

import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from sdd_core.codec import canonical, flag, integer, object_json, sequence, text, workflow_load
from sdd_core.graph import validate
from sdd_core.models import Json, Step, Workflow
from sdd_core.ports import Conflict
from sdd_core.profiles import resolve_profiles
from sdd_runtime.cli import registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write, revision
from sdd_runtime.profiles import load_profiles
from sdd_storage.store import Store
from sdd_workflows.templates import feature, interview, main_flow

from sdd_ui.preview import load_preview
from sdd_ui.subscriptions import read_codex_limits
from sdd_ui.workspace import WorkspaceCatalog


class WorkspaceService:
    def __init__(self, database: Path, config: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.engine = Engine(Store(database))
        self.preview = load_preview(database.resolve().with_suffix(".preview.json"))
        self.catalog = WorkspaceCatalog(self.engine.store)
        self.settings_path = database.resolve().with_suffix(".ui.json")
        self.config = config or database.resolve().with_suffix(".profiles.json")
        self.settings = (
            object_json(self.settings_path.read_text(encoding="utf-8"))
            if self.settings_path.exists()
            else {"running": False, "max_calls": 40, "max_planning_calls": 8}
        )
        self.settings["running"] = False
        self.handlers = registry(self.config if self.config.exists() else None)
        self.coordinator = Coordinator(
            self.engine, self.handlers, database.with_suffix(".health.json")
        )
        self.error: str | None = None
        self.last_tick: float | None = None
        self.quit = threading.Event()
        self.apply_limits()

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
            "settings": self.settings,
            "last_transition": last,
            "last_tick": self.last_tick,
            "error": self.error,
            "definitions": [
                {"digest": key, "workflow": asdict(flow)}
                for key, flow in self.engine.store.definitions()
            ],
            "profile_config": object_json(self.config.read_text(encoding="utf-8"))
            if self.config.exists()
            else {"schema": 1, "runners": {}, "profiles": {}},
            "profiles": [asdict(item) for item in self.handlers.manifests()],
            "active_processes": len(self.coordinator.live),
            "preview": self.preview,
            "projects": self.catalog.projects(),
            "task_metadata": self.catalog.task_metadata(),
            "locations": locations,
            "agent_discovery": self.catalog.discovery,
            "usage": self.catalog.usage(),
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
        }

    def mutate(self, action: str, doc: dict[str, Json]) -> object:
        if action == "interactive-demo":
            language = text(doc.get("language", "ru"), "language")
            prompt = (
                "Какой режим работы выбрать? Рекомендую подтверждение перед изменениями. Опишите ваш выбор."
                if language == "ru"
                else "Which working mode should we use? I recommend approval before changes. Describe your choice."
            )
            flow = Workflow(
                "interactive-demo",
                "answer",
                (
                    Step(
                        "answer",
                        "human",
                        prompt=prompt,
                        transitions=(("answered", "finish"),),
                        config=canonical(
                            {
                                "choices": [
                                    "С подтверждением перед изменениями",
                                    "Самостоятельно в рамках задачи",
                                ]
                                if language == "ru"
                                else ["Approval before changes", "Work within the agreed scope"]
                            }
                        ),
                    ),
                    Step("finish", "finish"),
                ),
            )
            identifier = "question-" + uuid.uuid4().hex[:8]
            root = self.engine.store.path.parent / "question-examples" / identifier
            root.mkdir(parents=True)
            run = self.engine.create(
                identifier,
                self.engine.store.publish(flow),
                root,
                "Interactive question example; no model calls or project edits.",
                revision(root),
                time.time(),
            )
            self.engine.command(run.id, "resume", uuid.uuid4().hex, run.version, time.time())
            self.catalog.save_task(
                run.id,
                {
                    "title": "Пример вопроса команды"
                    if language == "ru"
                    else "Team question example",
                    "language": language,
                    "project": "",
                },
            )
            run = self.engine.dispatch(run.id, time.time(), uuid.uuid4().hex)
            return asdict(run)
        if action == "project":
            return self.catalog.save_project(doc)
        if action == "discover":
            return self.catalog.discover()
        if action == "subscription":
            import queue

            candidates: list[tuple[str, ...]] = []
            if self.config.exists():
                candidates = [
                    (runner.executable, *runner.arguments)
                    for runner in load_profiles(self.config).runners.values()
                    if runner.adapter == "codex"
                ]
            if not candidates:
                from sdd_providers.catalog import adapters
                from sdd_runtime.discovery import discover

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
        if action == "message":
            return asdict(
                self.engine.message(
                    text(doc.get("id"), "id"),
                    text(doc.get("message"), "message"),
                    text(doc.get("request_id"), "request_id"),
                    integer(doc.get("version"), "version"),
                    time.time(),
                )
            )
        if action == "recover":
            return asdict(
                self.engine.request_recovery(
                    text(doc.get("id"), "id"), integer(doc.get("version"), "version"), time.time()
                )
            )
        if action == "template":
            name = text(doc.get("name"), "name")
            if name == "demo":
                import sys

                flow = Workflow(
                    "demo",
                    "check",
                    (
                        Step(
                            "check",
                            "check",
                            "command",
                            transitions=(("passed", "finish"),),
                            required=True,
                            gate=True,
                            config=canonical({"argv": [sys.executable, "-c", "print('Verified')"]}),
                        ),
                        Step("finish", "finish"),
                    ),
                    max_planning_calls=0,
                )
            else:
                flow = {"feature": feature, "main-flow": main_flow, "interview": interview}[name]()
            project = self.catalog.project(text(doc.get("project", ""), "project"))
            if doc.get("language", "ru") == "ru":
                labels = {
                    "interview": "Ответьте на вопросы по спецификации.",
                    "answer": "Ответьте на текущий вопрос.",
                    "approve": "Подтвердите задачу и критерии приёмки.",
                }
                flow = replace(
                    flow,
                    steps=tuple(
                        replace(step, prompt=labels.get(step.id, step.prompt))
                        if step.kind == "human"
                        else step
                        for step in flow.steps
                    ),
                )
            if project.get("checks"):
                flow = replace(
                    flow,
                    steps=tuple(
                        replace(
                            step,
                            config=canonical(
                                {
                                    **object_json(step.config),
                                    "argv": project["checks"],
                                }
                            ),
                        )
                        if step.kind == "check" and step.handler == "command"
                        else step
                        for step in flow.steps
                    ),
                )
            return asdict(flow)
        if action in ("validate", "publish"):
            flow = workflow_load(canonical(doc["workflow"]))
            if self.config.exists():
                flow = resolve_profiles(flow, load_profiles(self.config).profiles)
            validate(flow)
            # Require every executable step's handler before declaring it runnable.
            from sdd_core.sdk import handler_key

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
                "digest": self.engine.store.publish(flow) if action == "publish" else None,
            }
        if action == "create":
            project_id = text(doc.get("project", ""), "project")
            project = self.catalog.project(project_id)
            if project_id and not project:
                raise ValueError("Unknown project")
            workspace = Path(
                text(project.get("workspace", doc.get("workspace")), "workspace")
            ).resolve(strict=True)
            if self.engine.store.path.is_relative_to(workspace):
                raise ValueError("Keep the UI database outside the task workspace")
            language = text(doc.get("language", project.get("language", "ru")), "language")
            if language not in ("ru", "en"):
                raise ValueError("Unsupported response language")
            context = text(doc.get("context", ""), "context")
            context = (
                "Response language: "
                + ("Russian" if language == "ru" else "English")
                + ". Write user-facing questions, summaries and explanations in this language; keep protocol keys in English.\n"
                + context
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
        if action in ("pause", "resume", "retry", "stop", "answer"):
            identifier = text(doc.get("id"), "id")
            version = integer(doc.get("version"), "version")
            if action == "answer":
                if self.engine.store.get(identifier).version != version:
                    raise Conflict("Stale answer; refresh the task")
                self.coordinator.answer(
                    identifier,
                    text(doc.get("outcome"), "outcome"),
                    text(doc.get("answer"), "answer"),
                    time.time(),
                )
                return asdict(self.engine.store.get(identifier))
            return asdict(
                self.engine.command(
                    identifier,
                    action,
                    text(doc.get("request_id", uuid.uuid4().hex), "request_id"),
                    version,
                    time.time(),
                )
            )
        if action == "queue":
            if flag(doc.get("running"), "running") and self.error:
                self.coordinator.close()
                self.coordinator = Coordinator(self.engine, self.handlers)
                self.coordinator.restore(time.time())
                self.error = None
            self.settings["running"] = flag(doc.get("running"), "running")
            atomic_write(self.settings_path, canonical(self.settings))
            return self.settings
        if action == "budget":
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
            return self.settings
        if action == "profiles":
            if self.coordinator.live:
                raise Conflict("Wait for active executions before changing profiles")
            candidate = self.config.with_suffix(".candidate.json")
            try:
                atomic_write(candidate, canonical(doc["config"]))
                load_profiles(candidate)
                handlers = registry(candidate)
                atomic_write(self.config, candidate.read_text(encoding="utf-8"))
                self.handlers = handlers
                self.coordinator.registry = handlers
            finally:
                candidate.unlink(missing_ok=True)
            return [asdict(item) for item in self.handlers.manifests()]
        raise ValueError("Unknown action")

    def work(self) -> None:
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
                            for identifier in tuple(self.coordinator.live):
                                self.coordinator.collect(identifier, time.time())
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
