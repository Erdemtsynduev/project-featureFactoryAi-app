"""Work use cases: create, control, answer, approve a breakdown, recover.

Every mutation goes through a versioned engine command. Decomposition is
deterministic: a feature's breakdown step declares structured tickets, the operator
approves the specification and the tickets, and only then are the tickets admitted
as paused child runs with their dependencies and owned repositories. Admission is
idempotent per ticket. On approval the specification and the breakdown become the
feature's artifacts, kept by the factory, never written into the project.
"""

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import flag, integer, mapping, sequence, text
from sdd_core.memory import TicketDraft, tickets_of
from sdd_core.models import Json, Run
from sdd_core.ports import Conflict
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_workflows.templates import question_example

from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.diagnostics import record
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    INTENT_KIND,
    INTENTS,
    LANGUAGES,
    TaskRecord,
    language_rule,
    ticket_places,
)

# Input a ticket's brief leaves free for task memory, handoffs and operator guidance.
TICKET_MEMORY_CHARS = 8000

CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split(),
        strict=True,
    )
)


def ticket_scope(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    """The repositories a ticket owns, so independent tickets run side by side.

    Each owned path maps to the Git repository folder that contains it (one or two
    levels below the workspace). Any path outside a known repository means the
    ticket claims the whole workspace.
    """
    found: set[str] = set()
    for raw in paths:
        parts = Path(raw.replace("\\", "/")).parts
        if not parts or Path(raw).is_absolute() or ".." in parts:
            return ()
        for depth in (2, 1):
            if len(parts) >= depth and (root.joinpath(*parts[:depth]) / ".git").exists():
                found.add("/".join(parts[:depth]))
                break
        else:
            return ()
    # Nested repositories collapse into the outer one: scopes must not contain each other.
    return tuple(
        sorted(
            path
            for path in found
            if not any(other != path and path.startswith(other + "/") for other in found)
        )
    )


def slug(title: str, suffix: str) -> str:
    """A readable run id from a title (Cyrillic is transliterated) plus a unique suffix."""
    latin = "".join(CYRILLIC.get(char, char) for char in title.lower())
    base = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")[:48].strip("-") or "task"
    return f"{base}-{suffix}"


def tickets_markdown(title: str, drafts: tuple[TicketDraft, ...], ids: dict[str, str]) -> str:
    lines = [f"# Тикеты · {title}", ""]
    for draft in drafts:
        lines += [f"## {draft.id} · {draft.title}", f"Задача: `{ids[draft.id]}`", ""]
        if draft.goal:
            lines += [draft.goal, ""]
        if draft.depends_on:
            lines += ["Зависит от: " + ", ".join(draft.depends_on), ""]
        if draft.paths:
            lines += ["Владеет: " + ", ".join(draft.paths), ""]
        lines += ["Приёмка:", *[f"- {item}" for item in draft.acceptance], ""]
    return "\n".join(lines)


class TaskService:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
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
            language_rule(language) + text(doc.get("context", ""), "context"),
            revision(workspace),
            time.time(),
            tuple(text(x, "dependency") for x in sequence(doc.get("dependencies", []))),
        )
        self.catalog.save_task(
            run.id,
            TaskRecord(
                project=project_id,
                title=title or run.id,
                kind=INTENT_KIND.get(intent, "task"),
                language=language,
                intent=intent,
            ),
        )
        self.bind(run.id)
        self.log.record("task_created", run=run.id, intent=intent or "definition")
        return asdict(run)

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
        self.catalog.save_task(run.id, TaskRecord(title=title, language=language))
        return asdict(self.engine.dispatch(run.id, time.time(), uuid.uuid4().hex))

    def rename(self, doc: dict[str, Json]) -> dict[str, Json]:
        """Correct a task's title after creation."""
        identifier = text(doc.get("id"), "id")
        title = text(doc.get("title"), "title").strip()
        if not title:
            raise ValueError("A title is required")
        current = self.catalog.task(identifier)
        self.engine.store.get(identifier)
        record = current.changed(title=title[:200])
        self.catalog.update_task(identifier, record)
        self.log.record("task_renamed", run=identifier)
        return record.document()

    def close(self, doc: dict[str, Json]) -> dict[str, object]:
        """Close a parent before all its children are done: it counts as delivered
        with what is done, and each unfinished child becomes top-level work that
        remembers where it came from. Nothing runs, stops or is removed."""
        identifier = text(doc.get("id"), "id")
        if self.engine.store.get(identifier).status != "accepted":
            raise ValueError("Only work whose own run is accepted can be closed")
        records = self.catalog.tasks()
        children = [key for key, item in records.items() if item.parent == identifier]
        if not children:
            raise ValueError("Only work with tickets can be closed")
        with self.engine.store.unit() as db:
            status = {run.id: run.status for run in db.runs()}
        below: dict[str, list[str]] = {}
        for key, item in records.items():
            below.setdefault(item.parent, []).append(key)

        def finished(key: str, seen: frozenset[str]) -> bool:
            """Accepted, and so is everything below it unless it was closed."""
            if status.get(key) != "accepted" or key in seen:
                return status.get(key) == "accepted"
            return records[key].closed or all(
                finished(child, seen | {key}) for child in below.get(key, ())
            )

        detached = [key for key in children if not finished(key, frozenset())]
        for key in detached:
            self.catalog.update_task(key, records[key].changed(parent="", origin=identifier))
        self.catalog.update_task(identifier, records[identifier].changed(closed=True))
        self.log.record("work_closed", run=identifier, detached=list[Json](detached))
        return {"closed": identifier, "detached": list[Json](detached)}

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

    def bulk(self, command: str, doc: dict[str, Json]) -> dict[str, object]:
        """Resume or pause many tasks of one project in one operator action.

        `scope` "startable" resumes paused tasks whose dependencies are accepted;
        "all" resumes every paused task (the rest wait for their dependencies).
        Optional `kind`, `plan` and `ids` narrow it to what the operator selected
        (the board filter, a plan row, one task). `with_dependencies` also resumes
        the paused prerequisites of every unfinished selected task, transitively
        within the project, so work that waits on other work moves.
        Each task is commanded at its own current version; moved tasks are skipped.
        A ticket its plan marks HITL resumes only when named in `ids`: a person takes
        part in it, so it never starts as part of a whole plan.
        """
        if command not in ("resume", "pause"):
            raise ValueError("Bulk command must be resume or pause")
        project_id = text(doc.get("project", ""), "project")
        scope = text(doc.get("scope", "startable"), "scope")
        if scope not in ("startable", "all"):
            raise ValueError("Unknown scope")
        kind = text(doc.get("kind", ""), "kind")
        plan = text(doc.get("plan", ""), "plan")
        ids = {text(x, "id") for x in sequence(doc.get("ids", []))}
        with_dependencies = flag(doc.get("with_dependencies", False), "with_dependencies")
        records = self.catalog.tasks()

        def wanted(run: Run) -> bool:
            item = records.get(run.id, TaskRecord())
            return (
                (not kind or item.kind == TaskRecord.load({"kind": kind}).kind)
                and (not plan or item.plan == plan)
                and (not ids or run.id in ids)
            )

        project = self.project_runs(project_id)
        selected = [run for run in project if wanted(run)]
        chosen = [run for run in selected if self._eligible(command, run, scope)]
        if command == "resume" and with_dependencies:
            unfinished = [run for run in selected if run.status != "accepted"]
            chosen += self._prerequisites(unfinished, {run.id for run in project})
        held: list[str] = []
        if command == "resume":
            people = self.hitl(records)
            held = [run.id for run in chosen if run.id in people and run.id not in ids]
            chosen = [run for run in chosen if run.id not in held]
        done: list[str] = []
        skipped = 0
        for run in chosen:
            try:
                self.engine.command(run.id, command, uuid.uuid4().hex, run.version, time.time())
                done.append(run.id)
            except (ValueError, Conflict):
                skipped += 1
        self.log.record(
            f"bulk_{command}",
            project=project_id,
            scope=scope,
            task_kind=kind,
            plan=plan,
            tasks=len(ids),
            with_dependencies=with_dependencies,
            count=len(done),
            held=list[Json](held),
        )
        return {"changed": list[Json](done), "skipped": skipped, "held": list[Json](held)}

    def hitl(self, records: dict[str, TaskRecord]) -> frozenset[str]:
        """Tickets whose approved plan says a person must take part in them."""
        found: set[str] = set()
        for parent in {item.parent for item in records.values() if item.parent}:
            breakdown = self.catalog.artifacts(parent).get("tickets")
            if breakdown is not None:
                places = ticket_places(breakdown.data)
                found.update(run for run, place in places.items() if place["hitl"] is True)
        return frozenset(found)

    def _prerequisites(self, selected: list[Run], project: set[str]) -> list[Run]:
        """Resumable prerequisites of `selected` outside it, within the project."""
        seen = {run.id for run in selected}
        found: list[Run] = []
        frontier = list(selected)
        with self.engine.store.unit() as db:
            while frontier:
                for dependency in db.dependencies(frontier.pop().id):
                    if dependency.id in seen or dependency.id not in project:
                        continue
                    seen.add(dependency.id)
                    frontier.append(dependency)
                    if self._eligible("resume", dependency, "all"):
                        found.append(dependency)
        return found

    def _eligible(self, command: str, run: Run, scope: str) -> bool:
        if run.status == "accepted" or run.active is not None:
            return False
        if command == "pause":
            return not run.paused
        if not run.paused or run.status == "blocked":
            return False
        if scope == "all":
            return True
        with self.engine.store.unit() as db:
            return all(dependency.status == "accepted" for dependency in db.dependencies(run.id))

    def project_runs(self, project_id: str) -> list[Run]:
        """Runs of a project: by their record, else by workspace (older imports)."""
        project = self.catalog.project(project_id)
        records = self.catalog.tasks()
        workspaces = {str(p["id"]): str(p["workspace"]) for p in self.catalog.projects()}
        with self.engine.store.unit() as db:
            runs = db.runs()
            locations = dict(db.locations())

        def owner(run: Run) -> str:
            declared = records.get(run.id, TaskRecord()).project
            if declared in workspaces:
                return declared
            return next((k for k, v in workspaces.items() if v == locations[run.id]), "")

        if project_id and not project:
            raise ValueError("Unknown project")
        return [run for run in runs if owner(run) == project_id]

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
        # Publish the ticket flows and build every brief before the answer, so what
        # can be refused is refused while the feature still awaits approval.
        definitions = self._ticket_definitions(run_id, drafts)
        contexts = self._ticket_contexts(run_id, drafts, definitions)
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
        created = self._admit(run, drafts, definitions, contexts) if drafts else []
        self.log.record("answered", run=run_id, outcome=outcome, tickets=len(created))
        return {**asdict(run), "admitted": list[Json](created)}

    def preview(self, run_id: str) -> list[dict[str, object]]:
        """Tickets awaiting approval on a feature, for the approval dialog."""
        try:
            return [asdict(draft) for draft in tickets_of(self.engine.facts(run_id))]
        except ValueError:
            return []

    def _ticket_definitions(self, run_id: str, drafts: tuple[TicketDraft, ...]) -> dict[str, str]:
        """Each ticket's workflow: the project's ticket template with its owners' checks."""
        if not drafts:
            return {}
        item = self.catalog.task(run_id)
        with self.engine.store.unit() as db:
            root = Path(db.location(run_id)[0])
        return {
            draft.id: self.flows.ensure(
                "ticket", item.project, item.language, ticket_scope(root, draft.paths)
            )
            for draft in drafts
        }

    def _ticket_contexts(
        self, run_id: str, drafts: tuple[TicketDraft, ...], definitions: dict[str, str]
    ) -> dict[str, str]:
        """Each ticket's brief within its workflow's input budget, room left for memory."""
        if not drafts:
            return {}
        rule = language_rule(self.catalog.task(run_id).language)
        specification = self.specification(run_id)
        contexts: dict[str, str] = {}
        for draft in drafts:
            budget = self.engine.store.workflow(definitions[draft.id]).max_input_chars
            limit = budget - len(rule) - TICKET_MEMORY_CHARS
            contexts[draft.id] = rule + draft.context(specification, limit)
        return contexts

    def specification(self, run_id: str) -> str:
        """The latest specification the feature's specification step produced, whole."""
        for result in self.engine.outputs(run_id, "specification"):
            if result.outcome == "done":
                return result.reason
        return ""

    def readmit(self) -> list[str]:
        """Finish admissions an interrupted approval left undone.

        Approval accepts the feature first and then creates its tickets; the tickets
        artifact is saved last. An accepted feature with an approved breakdown but no
        tickets artifact was cut short, so its remaining tickets are admitted now.
        """
        with self.engine.store.unit() as db:
            accepted = [run for run in db.runs() if run.status == "accepted"]
        created: list[str] = []
        for run in accepted:
            if "tickets" in self.catalog.artifacts(run.id):
                continue
            breakdown = next(
                (r for r in self.engine.outputs(run.id, "tickets") if r.outcome == "done"), None
            )
            if breakdown is None:
                continue
            drafts = tickets_of(breakdown.data)
            if not drafts:
                continue
            definitions = self._ticket_definitions(run.id, drafts)
            contexts = self._ticket_contexts(run.id, drafts, definitions)
            created += self._admit(run, drafts, definitions, contexts)
        return created

    def _admit(
        self,
        parent: Run,
        drafts: tuple[TicketDraft, ...],
        definitions: dict[str, str],
        contexts: dict[str, str],
    ) -> list[str]:
        feature = self.catalog.task(parent.id)
        with self.engine.store.unit() as db:
            root = Path(db.location(parent.id)[0])
        specification = self.specification(parent.id)
        ids = {draft.id: self._child_id(parent.id, draft.id) for draft in drafts}
        created: list[str] = []
        for draft in drafts:
            child = ids[draft.id]
            try:
                self.engine.store.get(child)
                continue  # admitted before: approval is idempotent per ticket
            except KeyError:
                pass
            run = self.engine.create(
                child,
                definitions[draft.id],
                root,
                contexts[draft.id],
                None,
                time.time(),
                tuple(ids[dependency] for dependency in draft.depends_on),
                ticket_scope(root, draft.paths),
            )
            self.catalog.save_task(
                run.id,
                TaskRecord(
                    project=feature.project,
                    title=draft.title,
                    kind="ticket",
                    language=feature.language,
                    parent=parent.id,
                    plan=feature.plan,
                ),
            )
            self.bind(run.id)
            created.append(run.id)
        title = feature.title or parent.id
        self.catalog.save_artifact(Artifact(parent.id, "specification", specification))
        self.catalog.save_artifact(
            Artifact(
                parent.id,
                "tickets",
                tickets_markdown(title, drafts, ids),
                [{**asdict(draft), "run": ids[draft.id]} for draft in drafts],
            )
        )
        self._export(parent.id)
        self.log.record("tickets_admitted", run=parent.id, tickets=list[Json](created))
        return created

    # Artifacts -------------------------------------------------------------------

    def artifacts_folder(self, run_id: str) -> Path:
        """Where the factory exports a feature's documents for people to read."""
        project = self.catalog.task(run_id).project or "_"
        return self.engine.store.path.parent / "artifacts" / project / run_id

    def _export(self, run_id: str) -> None:
        folder = self.artifacts_folder(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        title = self.catalog.task(run_id).title or run_id
        stored = self.catalog.artifacts(run_id)
        if "specification" in stored:
            text = f"# {title}\n\n{stored['specification'].content}\n"
            (folder / "spec.md").write_text(text, encoding="utf-8")
        if "tickets" in stored:
            (folder / "tickets.md").write_text(stored["tickets"].content, encoding="utf-8")

    def documents(self, run_id: str) -> dict[str, object]:
        """The feature's specification (PRD) and ticket breakdown: approved or in progress."""
        stored = self.catalog.artifacts(run_id)
        approved = "specification" in stored
        specification = stored["specification"].content if approved else self.specification(run_id)
        tickets = stored["tickets"].data if "tickets" in stored else self.preview(run_id)
        return {
            "specification": specification,
            "tickets": tickets,
            "approved": approved,
            "folder": str(self.artifacts_folder(run_id)) if approved else "",
        }

    @staticmethod
    def _child_id(parent: str, ticket: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", ticket).strip("-") or "ticket"
        return f"{parent[: 95 - len(safe)]}-{safe}"
