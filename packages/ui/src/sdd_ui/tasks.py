"""Task use cases of the operator UI: create, control, answer, decompose, recover.

Every mutation goes through a versioned engine command. Decomposition is
deterministic: a requirement's tickets step declares structured tickets, the
operator approves them, and only then are they admitted as paused child ticket
runs with their dependencies. Admission is idempotent per ticket id.
"""

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import integer, mapping, object_json, result_load, sequence, text
from sdd_core.memory import TicketDraft, tickets_of
from sdd_core.models import Json, Run
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_workflows.templates import question_example

from sdd_ui.diagnostics import record
from sdd_ui.flightlog import FlightLog
from sdd_ui.flows import INTENTS, FlowLibrary
from sdd_ui.workspace import WorkspaceCatalog

KIND_OF_INTENT = {"requirement": "requirement", "ticket": "ticket"}
SPECIFICATION_CHARS = 20000
LANGUAGES = {"ru": "Russian", "en": "English"}


CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split(),
        strict=True,
    )
)


def slug(title: str, suffix: str) -> str:
    """A readable run id from a title (Cyrillic is transliterated) plus a unique suffix."""
    latin = "".join(CYRILLIC.get(char, char) for char in title.lower())
    base = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")[:48].strip("-") or "task"
    return f"{base}-{suffix}"


class TaskService:
    def __init__(
        self,
        engine: Engine,
        catalog: WorkspaceCatalog,
        flows: FlowLibrary,
        log: FlightLog,
        bind: Callable[[str], None],
    ) -> None:
        self.engine, self.catalog, self.flows, self.log = engine, catalog, flows, log
        self.bind = bind

    # Creation -----------------------------------------------------------------

    def create(self, doc: dict[str, Json]) -> dict[str, object]:
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
        if language not in LANGUAGES:
            raise ValueError("Unsupported response language")
        title = text(doc.get("title", ""), "title").strip()
        identifier = text(doc.get("id", ""), "id").strip() or slug(title, uuid.uuid4().hex[:4])
        intent = text(doc.get("intent", ""), "intent")
        if intent:
            if intent not in INTENTS:
                raise ValueError("Unknown task type")
            definition = self.flows.ensure(intent, project_id, language)
        else:
            definition = text(doc.get("definition"), "definition")
        run = self.engine.create(
            identifier,
            definition,
            workspace,
            self._context(language, text(doc.get("context", ""), "context")),
            revision(workspace),
            time.time(),
            tuple(text(x, "dependency") for x in sequence(doc.get("dependencies", []))),
        )
        metadata: dict[str, Json] = {
            "project": project_id,
            "language": language,
            "title": title or run.id,
            "kind": KIND_OF_INTENT.get(intent, "task"),
        }
        if intent:
            metadata["intent"] = intent
        self.catalog.save_task(run.id, metadata)
        self.bind(run.id)
        self.log.record("task_created", run=run.id, intent=intent or "definition")
        return asdict(run)

    @staticmethod
    def _context(language: str, body: str) -> str:
        return (
            f"Response language: {LANGUAGES[language]}. Write user-facing questions, summaries"
            " and explanations in this language; keep protocol keys in English.\n" + body
        )

    def demo(self, doc: dict[str, Json]) -> dict[str, object]:
        """A human-only task in a sample folder: shows a question without a model."""
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
        title = "Пример вопроса команды" if language == "ru" else "Team question example"
        self.catalog.save_task(
            run.id, {"title": title, "language": language, "project": "", "kind": "task"}
        )
        return asdict(self.engine.dispatch(run.id, time.time(), uuid.uuid4().hex))

    # Control ------------------------------------------------------------------

    def command(self, command: str, doc: dict[str, Json]) -> dict[str, object]:
        run = self.engine.command(
            text(doc.get("id"), "id"),
            command,
            text(doc.get("request_id", uuid.uuid4().hex), "request_id"),
            integer(doc.get("version"), "version"),
            time.time(),
        )
        self.log.record("command", run=run.id, command=command, status=run.status)
        return asdict(run)

    def message(self, doc: dict[str, Json]) -> dict[str, object]:
        return asdict(
            self.engine.message(
                text(doc.get("id"), "id"),
                text(doc.get("message"), "message"),
                text(doc.get("request_id"), "request_id"),
                integer(doc.get("version"), "version"),
                time.time(),
            )
        )

    def recover(self, doc: dict[str, Json]) -> dict[str, object]:
        """Hand the incident to the read-only recovery step, then leave the task paused."""
        run_id = text(doc.get("id"), "id")
        version = integer(doc.get("version"), "version")
        briefing = record(self.engine, self.log, run_id).briefing()
        guided = self.engine.message(run_id, briefing, uuid.uuid4().hex, version, time.time())
        state = self.engine.request_recovery(run_id, guided.version, time.time())
        self.log.record("recovery_requested", run=run_id, step=state.step)
        return asdict(state)

    # Answers and decomposition ------------------------------------------------

    def answer(self, doc: dict[str, Json]) -> dict[str, object]:
        run_id = text(doc.get("id"), "id")
        outcome = text(doc.get("outcome"), "outcome")
        drafts = tickets_of(self.engine.facts(run_id)) if outcome == "approved" else ()
        # Publish the ticket flow before the answer, so approval never half-applies.
        definition = self._ticket_definition(run_id) if drafts else ""
        choices = {
            key: text(value, "choice") for key, value in mapping(doc.get("choices", {})).items()
        }
        # A bare decision (approve, reject) needs no typed text; record the choice itself.
        reply = text(doc.get("answer", ""), "answer").strip() or (
            "" if choices else f"Operator decision: {outcome}"
        )
        run = self.engine.answer(
            run_id,
            outcome,
            reply,
            choices,
            integer(doc.get("version"), "version"),
            time.time(),
        )
        created = self._admit(run, drafts, definition) if drafts else []
        self.log.record("answered", run=run_id, outcome=outcome, tickets=len(created))
        return {**asdict(run), "admitted": list[Json](created)}

    def preview(self, run_id: str) -> list[dict[str, object]]:
        """Tickets awaiting approval on a requirement, for the approval dialog."""
        try:
            return [asdict(draft) for draft in tickets_of(self.engine.facts(run_id))]
        except ValueError:
            return []

    def _ticket_definition(self, run_id: str) -> str:
        meta = self.catalog.task_metadata().get(run_id, {})
        return self.flows.ensure(
            "ticket", str(meta.get("project", "")), str(meta.get("language", "ru"))
        )

    def _specification(self, run_id: str) -> str:
        """The latest specification the planner produced before its breakdown."""
        with self.engine.store.unit() as db:
            documents = db.recent_results(run_id, 12)
        results = [result_load(document) for document in documents]
        breakdown = next(
            (i for i, r in enumerate(results) if object_json(r.data or "{}").get("tickets")), None
        )
        if breakdown is None:
            return ""
        for result in results[breakdown + 1 :]:
            if result.outcome == "done" and "answer" not in object_json(result.data or "{}"):
                return result.reason[:SPECIFICATION_CHARS]
        return ""

    def _admit(self, parent: Run, drafts: tuple[TicketDraft, ...], definition: str) -> list[str]:
        meta = self.catalog.task_metadata().get(parent.id, {})
        with self.engine.store.unit() as db:
            root = Path(db.location(parent.id)[0])
        specification = self._specification(parent.id)
        ids = {draft.id: self._child_id(parent.id, draft.id) for draft in drafts}
        created: list[str] = []
        for draft in drafts:
            child = ids[draft.id]
            try:
                self.engine.store.get(child)
                continue  # admitted before: approval is idempotent per ticket
            except KeyError:
                pass
            language = str(meta.get("language", "ru"))
            run = self.engine.create(
                child,
                definition,
                root,
                self._context(language, draft.context(specification)),
                None,
                time.time(),
                tuple(ids[dependency] for dependency in draft.depends_on),
            )
            self.catalog.save_task(
                run.id,
                {
                    "project": meta.get("project", ""),
                    "language": language,
                    "title": draft.title,
                    "kind": "ticket",
                    "parent": parent.id,
                    "plan": meta.get("plan", ""),
                },
            )
            self.bind(run.id)
            created.append(run.id)
        self.log.record("tickets_admitted", run=parent.id, tickets=list[Json](created))
        return created

    @staticmethod
    def _child_id(parent: str, ticket: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", ticket).strip("-") or "ticket"
        return f"{parent[: 95 - len(safe)]}-{safe}"
