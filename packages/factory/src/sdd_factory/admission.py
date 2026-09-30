"""Approving a breakdown: checking its drafts, then admitting each as a child run.

A feature's breakdown becomes tickets and a draft's becomes features (`Children`):
the same approval, the same artifact, the same idempotent admission. What differs is
a row of that table and the brief each kind of child is given.
"""

import re
import time
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import decode, digest, encode, integer, mapping, text
from sdd_core.models import Json, Run
from sdd_core.ports import Conflict
from sdd_core.tickets import (
    ScopeOf,
    TicketDraft,
    covers_rows,
    drafts_of,
    one_repository,
    waits_for_known,
)
from sdd_core.tracking import WorkItem
from sdd_runtime.engine import Engine

from sdd_factory.board import live_tickets, source_tickets
from sdd_factory.briefs import feature_brief, source_work
from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.documents import FeatureDocuments
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    PLANNING_SCOPE,
    Children,
    Kind,
    TaskRecord,
    children_of,
    language_rule,
)

# Input a ticket's brief leaves free for task memory, handoffs and operator guidance.
TICKET_MEMORY_CHARS = 8000
# What a breakdown is called in the document written for people.
HEADINGS = {"tickets": "Тикеты", "features": "Фичи"}

type Brief = Callable[[str, TicketDraft, int], str]


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


def breakdown_markdown(
    heading: str, title: str, drafts: tuple[TicketDraft, ...], ids: dict[str, str]
) -> str:
    lines = [f"# {heading} · {title}", ""]
    for draft in drafts:
        lines += [f"## {draft.id} · {draft.title}", f"Задача: `{ids[draft.id]}`", ""]
        if draft.goal:
            lines += [draft.goal, ""]
        if draft.depends_on:
            lines += ["Зависит от: " + ", ".join(draft.depends_on), ""]
        if draft.paths:
            lines += ["Владеет: " + ", ".join(draft.paths), ""]
        if draft.covers:
            lines += ["Покрывает: " + ", ".join(draft.covers), ""]
        if draft.labels:
            lines += ["Теги: " + ", ".join(draft.labels), ""]
        if draft.acceptance:
            lines += ["Приёмка:", *[f"- {item}" for item in draft.acceptance], ""]
    return "\n".join(lines)


class BreakdownAdmission:
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
        # The brief of each kind of child: a new kind of child is a new row.
        self.briefs: dict[Kind, Brief] = {
            "ticket": self._ticket_brief,
            "feature": self._feature_brief,
        }

    def answer(self, doc: dict[str, Json]) -> dict[str, object]:
        run_id = text(doc.get("id"), "id")
        outcome = text(doc.get("outcome"), "outcome")
        policy = self.children(run_id)
        drafts = self.declared(run_id, policy) if outcome == "approved" else ()
        # Refused while the parent still awaits approval; rework sends the reason back.
        self.check(run_id, drafts)
        # Publish the children's flows and build every brief before the answer, so what
        # can be refused is refused while the parent still awaits approval.
        definitions = self.child_definitions(run_id, drafts)
        contexts = self.child_contexts(run_id, drafts, definitions)
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
        counted: dict[str, Json] = {policy.product: len(created)}
        self.log.record("answered", "info", run_id, outcome=outcome, **counted)
        return {**encode(run), "admitted": list[Json](created)}

    # What a parent's children are ---------------------------------------------------

    def children(self, run_id: str) -> Children:
        return children_of(self.catalog.task(run_id).kind)

    def declared(self, run_id: str, policy: Children) -> tuple[TicketDraft, ...]:
        """The drafts the parent's breakdown step declared, awaiting approval."""
        return drafts_of(self.engine.facts(run_id), policy.product)

    def check(self, run_id: str, drafts: tuple[TicketDraft, ...]) -> None:
        """The rules a breakdown of `run_id` must keep before it is approved."""
        if not self.children(run_id).planning:
            one_repository(drafts, self.scope_of(run_id))
        elif drafts:
            covers_rows(drafts, self.catalog.task(run_id).rows)
        waits_for_known(drafts, self.known_tickets(run_id))

    def known_tickets(self, run_id: str) -> set[str]:
        """Existing tickets from `run_id`'s source: what a new ticket's `after` may name."""
        with self.engine.store.unit() as db:
            runs = [run.id for run in db.runs()]
        return live_tickets(self.catalog.tasks(), runs, self.catalog.task(run_id).source)

    def scope_of(self, run_id: str) -> ScopeOf:
        """The repositories a ticket draft of `run_id` changes, within its workspace."""
        root = self.engine.root(run_id)
        return lambda draft: ticket_scope(root, draft.paths)

    def child_definitions(self, run_id: str, drafts: tuple[TicketDraft, ...]) -> dict[str, str]:
        """Each child's workflow: the project's template for its kind; a ticket's has the
        checks of the repositories it owns."""
        if not drafts:
            return {}
        item, policy = self.catalog.task(run_id), self.children(run_id)
        scope: ScopeOf = (lambda draft: ()) if policy.planning else self.scope_of(run_id)
        return {
            draft.id: self.flows.ensure(policy.kind, item.project, item.language, scope(draft))
            for draft in drafts
        }

    def child_contexts(
        self, run_id: str, drafts: tuple[TicketDraft, ...], definitions: dict[str, str]
    ) -> dict[str, str]:
        """Each child's brief within its workflow's input budget, room left for memory."""
        if not drafts:
            return {}
        rule = language_rule(self.catalog.task(run_id).language)
        brief = self.briefs[self.children(run_id).kind]
        contexts: dict[str, str] = {}
        for draft in drafts:
            budget = self.engine.store.workflow(definitions[draft.id]).max_input_chars
            contexts[draft.id] = rule + brief(
                run_id, draft, budget - len(rule) - TICKET_MEMORY_CHARS
            )
        return contexts

    def _ticket_brief(self, parent: str, draft: TicketDraft, limit: int) -> str:
        return draft.context(self.documents.specification(parent), limit)

    def _feature_brief(self, parent: str, draft: TicketDraft, limit: int) -> str:
        """The feature the lead cut, with the rows of its draft and the source's tickets."""
        stored = self.catalog.artifacts(parent).get("draft")
        if stored is None:
            raise ValueError(f"{parent} has no recorded draft to cut features from")
        records = self.catalog.tasks()
        with self.engine.store.unit() as db:
            status = {run.id: run.status for run in db.runs()}
        work = source_work(records, status, source_tickets(records, status, records[parent].source))
        brief = feature_brief(decode(WorkItem, stored.data), draft, work)
        if len(brief) > limit:
            raise ValueError(f"Feature {draft.id} exceeds its context budget; cut it smaller")
        return brief

    # Admission ----------------------------------------------------------------------

    def readmit(self) -> list[str]:
        """Finish admissions an interrupted approval left undone.

        Approval accepts the parent first and then creates its children; the breakdown
        artifact is saved last. An accepted parent with an approved breakdown but no
        artifact was cut short, so its remaining children are admitted now.
        """
        with self.engine.store.unit() as db:
            accepted = [run for run in db.runs() if run.status == "accepted"]
        created: list[str] = []
        for run in accepted:
            policy = self.children(run.id)
            if policy.product in self.catalog.artifacts(run.id):
                continue
            breakdown = self.engine.latest(run.id, policy.product)
            if breakdown is None:
                continue
            drafts = drafts_of(breakdown.data, policy.product)
            if not drafts:
                continue
            definitions = self.child_definitions(run.id, drafts)
            contexts = self.child_contexts(run.id, drafts, definitions)
            created += self._admit(run, drafts, definitions, contexts)
        return created

    def _admit(
        self,
        parent: Run,
        drafts: tuple[TicketDraft, ...],
        definitions: dict[str, str],
        contexts: dict[str, str],
    ) -> list[str]:
        ids = self.child_runs(parent.id, drafts)
        # Children of one repository start from the same revision: observe it once.
        revisions: dict[tuple[str, ...], str] = {}
        created = [
            draft.id
            for draft in drafts
            if self.admit_child(
                parent.id, draft, ids, definitions[draft.id], contexts[draft.id], revisions
            )
        ]
        specification = self.documents.specification(parent.id)
        if specification:
            self.catalog.save_artifact(Artifact(parent.id, "specification", specification))
        self.save_breakdown(parent.id, drafts, ids)
        self._hand_over(parent.id, drafts, ids)
        admitted = [ids[identifier] for identifier in created]
        product = self.children(parent.id).product
        listed: dict[str, Json] = {product: list[Json](admitted)}
        self.log.record(f"{product}_admitted", "info", parent.id, **listed)
        return admitted

    def child_runs(self, parent: str, drafts: tuple[TicketDraft, ...]) -> dict[str, str]:
        """Each draft's run id: the one the breakdown admitted it as, else a new child id."""
        admitted = {
            str(item.get("id")): str(item.get("run"))
            for item in self.catalog.breakdown(parent)
            if isinstance(item.get("run"), str)
        }
        return {
            draft.id: admitted.get(draft.id) or self._child_id(parent, draft.id) for draft in drafts
        }

    def admit_child(
        self,
        parent: str,
        draft: TicketDraft,
        ids: dict[str, str],
        definition: str,
        context: str,
        revisions: dict[tuple[str, ...], str] | None = None,
    ) -> bool:
        """Create one paused child run; False when it exists (admission is idempotent per
        child). `revisions` caches the revision of each scope within one admission."""
        child = ids[draft.id]
        try:
            self.engine.store.get(child)
            return False
        except KeyError:
            pass
        above, policy = self.catalog.task(parent), self.children(parent)
        root = self.engine.root(parent)
        # A ticket owns the repositories it changes; a child that only plans owns none.
        scope = (PLANNING_SCOPE,) if policy.planning else ticket_scope(root, draft.paths)
        cache = {} if revisions is None else revisions
        if scope not in cache:
            cache[scope] = self.engine.scope_revision(root, scope)
        run = self.engine.create(
            child,
            definition,
            root,
            context,
            cache[scope],
            time.time(),
            (*(ids[dependency] for dependency in draft.depends_on), *draft.after),
            scope,
        )
        self.catalog.save_task(
            run.id,
            TaskRecord(
                project=above.project,
                title=draft.title,
                kind=policy.kind,
                language=above.language,
                parent=parent,
                rows=draft.covers,
                source=above.source,
                labels=draft.labels or above.labels,
            ),
        )
        self.bind(run.id)
        return True

    def save_breakdown(
        self, parent: str, drafts: tuple[TicketDraft, ...], ids: dict[str, str]
    ) -> None:
        """The parent's breakdown artifact: its drafts with the runs they are, for people
        (Markdown, exported) and for the board (waves, needs)."""
        title = self.catalog.task(parent).title or parent
        product = self.children(parent).product
        self.catalog.save_artifact(
            Artifact(
                parent,
                product,
                breakdown_markdown(HEADINGS[product], title, drafts, ids),
                [{**encode(draft), "run": ids[draft.id]} for draft in drafts],
            )
        )
        self.documents.export(parent)

    def _hand_over(self, parent: str, drafts: tuple[TicketDraft, ...], ids: dict[str, str]) -> None:
        """Tell each feature that waits for `parent` what was planned, so its tickets can
        wait for these (`after`). Said once per feature: a repeated admission adds nothing."""
        records = self.catalog.tasks()
        with self.engine.store.unit() as db:
            waiting = [run for run, needed in db.dependency_edges() if needed == parent]
        words = "\n".join(
            [
                f"{records[parent].title or parent} ({parent}) is planned. Its work, by run id"
                " (a ticket that waits for one names it in `after`):",
                *(f"- {ids[draft.id]} — {draft.title}" for draft in drafts),
            ]
        )
        for run_id in waiting:
            if records.get(run_id, TaskRecord()).kind != "feature":
                continue
            run = self.engine.store.get(run_id)
            key = digest(f"{parent}:{run_id}:planned")[:64]
            try:
                self.engine.message(run_id, words, key, run.version, time.time())
            except Conflict:
                pass  # recorded before under this request id: told already
            except ValueError as error:
                self.log.record("hand_over_failed", "warning", run=run_id, error=str(error))

    @staticmethod
    def _child_id(parent: str, ticket: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", ticket).strip("-") or "ticket"
        return f"{parent[: 95 - len(safe)]}-{safe}"
