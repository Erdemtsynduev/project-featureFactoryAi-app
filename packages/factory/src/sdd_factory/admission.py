"""Approving a breakdown: checking its drafts, then admitting each ticket as a child run with its flow and brief."""

import re
import time
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import encode, integer, mapping, text
from sdd_core.models import Json, Run
from sdd_core.tickets import (
    ScopeOf,
    TicketDraft,
    one_repository,
    tickets_of,
    waits_for_known,
)
from sdd_runtime.engine import Engine

from sdd_factory.board import plan_tickets
from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.documents import FeatureDocuments
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    TaskRecord,
    language_rule,
)

# Input a ticket's brief leaves free for task memory, handoffs and operator guidance.
TICKET_MEMORY_CHARS = 8000


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


class TicketAdmission:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        flows: FlowLibrary,
        log: FlightLog,
        bind: Callable[[str], None],
        documents: FeatureDocuments,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.flows = flows
        self.log = log
        self.bind = bind
        self.documents = documents

    def answer(self, doc: dict[str, Json]) -> dict[str, object]:
        run_id = text(doc.get("id"), "id")
        outcome = text(doc.get("outcome"), "outcome")
        drafts = tickets_of(self.engine.facts(run_id)) if outcome == "approved" else ()
        # Refused while the feature still awaits approval; rework sends the reason back.
        one_repository(drafts, self.scope_of(run_id))
        waits_for_known(drafts, self.known_tickets(run_id))
        # Publish the ticket flows and build every brief before the answer, so what
        # can be refused is refused while the feature still awaits approval.
        definitions = self.ticket_definitions(run_id, drafts)
        contexts = self.ticket_contexts(run_id, drafts, definitions)
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
        return {**encode(run), "admitted": list[Json](created)}

    def known_tickets(self, run_id: str) -> set[str]:
        """Existing tickets of `run_id`'s plan: what a new ticket's `after` may name."""
        with self.engine.store.unit() as db:
            runs = [run.id for run in db.runs()]
        return plan_tickets(self.catalog.tasks(), runs, self.catalog.task(run_id).plan)

    def scope_of(self, run_id: str) -> ScopeOf:
        """The repositories a draft of `run_id`'s plan changes, within its workspace."""
        root = self.engine.root(run_id)
        return lambda draft: ticket_scope(root, draft.paths)

    def ticket_definitions(self, run_id: str, drafts: tuple[TicketDraft, ...]) -> dict[str, str]:
        """Each ticket's workflow: the project's ticket template with its owners' checks."""
        if not drafts:
            return {}
        item = self.catalog.task(run_id)
        scope = self.scope_of(run_id)
        return {
            draft.id: self.flows.ensure("ticket", item.project, item.language, scope(draft))
            for draft in drafts
        }

    def ticket_contexts(
        self, run_id: str, drafts: tuple[TicketDraft, ...], definitions: dict[str, str]
    ) -> dict[str, str]:
        """Each ticket's brief within its workflow's input budget, room left for memory."""
        if not drafts:
            return {}
        rule = language_rule(self.catalog.task(run_id).language)
        specification = self.documents.specification(run_id)
        contexts: dict[str, str] = {}
        for draft in drafts:
            budget = self.engine.store.workflow(definitions[draft.id]).max_input_chars
            limit = budget - len(rule) - TICKET_MEMORY_CHARS
            contexts[draft.id] = rule + draft.context(specification, limit)
        return contexts

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
            breakdown = self.engine.latest(run.id, "tickets")
            if breakdown is None:
                continue
            drafts = tickets_of(breakdown.data)
            if not drafts:
                continue
            definitions = self.ticket_definitions(run.id, drafts)
            contexts = self.ticket_contexts(run.id, drafts, definitions)
            created += self._admit(run, drafts, definitions, contexts)
        return created

    def _admit(
        self,
        parent: Run,
        drafts: tuple[TicketDraft, ...],
        definitions: dict[str, str],
        contexts: dict[str, str],
    ) -> list[str]:
        ids = self.ticket_runs(parent.id, drafts)
        created = [
            draft.id
            for draft in drafts
            if self.admit_ticket(parent.id, draft, ids, definitions[draft.id], contexts[draft.id])
        ]
        self.catalog.save_artifact(
            Artifact(parent.id, "specification", self.documents.specification(parent.id))
        )
        self.save_breakdown(parent.id, drafts, ids)
        admitted = [ids[identifier] for identifier in created]
        self.log.record("tickets_admitted", run=parent.id, tickets=list[Json](admitted))
        return admitted

    def breakdown(self, parent: str) -> list[dict[str, Json]]:
        """The approved plan's tickets as stored: each draft with the run it became."""
        stored = self.catalog.artifacts(parent).get("tickets")
        items = stored.data if stored and isinstance(stored.data, list) else []
        return [item for item in items if isinstance(item, dict)]

    def ticket_runs(self, parent: str, drafts: tuple[TicketDraft, ...]) -> dict[str, str]:
        """Each draft's run id: the one the plan admitted it as, else a new child id."""
        admitted = {
            str(item.get("id")): str(item.get("run"))
            for item in self.breakdown(parent)
            if isinstance(item.get("run"), str)
        }
        return {
            draft.id: admitted.get(draft.id) or self._child_id(parent, draft.id) for draft in drafts
        }

    def admit_ticket(
        self,
        parent: str,
        draft: TicketDraft,
        ids: dict[str, str],
        definition: str,
        context: str,
    ) -> bool:
        """Create one ticket's paused child run; False when it exists (admission is
        idempotent per ticket)."""
        child = ids[draft.id]
        try:
            self.engine.store.get(child)
            return False
        except KeyError:
            pass
        feature = self.catalog.task(parent)
        root = self.engine.root(parent)
        run = self.engine.create(
            child,
            definition,
            root,
            context,
            None,
            time.time(),
            (*(ids[dependency] for dependency in draft.depends_on), *draft.after),
            ticket_scope(root, draft.paths),
        )
        self.catalog.save_task(
            run.id,
            TaskRecord(
                project=feature.project,
                title=draft.title,
                kind="ticket",
                language=feature.language,
                parent=parent,
                plan=feature.plan,
            ),
        )
        self.bind(run.id)
        return True

    def save_breakdown(
        self, parent: str, drafts: tuple[TicketDraft, ...], ids: dict[str, str]
    ) -> None:
        """The plan's tickets artifact: its drafts with the runs they are, for people
        (Markdown, exported) and for the board (waves, needs)."""
        title = self.catalog.task(parent).title or parent
        self.catalog.save_artifact(
            Artifact(
                parent,
                "tickets",
                tickets_markdown(title, drafts, ids),
                [{**encode(draft), "run": ids[draft.id]} for draft in drafts],
            )
        )
        self.documents.export(parent)

    @staticmethod
    def _child_id(parent: str, ticket: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", ticket).strip("-") or "ticket"
        return f"{parent[: 95 - len(safe)]}-{safe}"
