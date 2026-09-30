"""Plan reviews: a plan lead's proposals for an approved ticket breakdown.

A review runs when a ticket's agent blocks it or right after a breakdown is
approved. Its agent only proposes `plan_changes`; a person decides. Every rule is
`sdd_core.plan_changes`; this service reads the plan, starts reviews and applies
an approved `Revision` through versioned engine commands, each idempotent by its
request id, with the review's `plan_changes` artifact written last as the marker
that the revision is complete.
"""

import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from sdd_core.codec import digest, encode, object_json, sequence, text
from sdd_core.editor import FlowChange
from sdd_core.machine import discardable
from sdd_core.models import Json, Run
from sdd_core.plan_changes import (
    PlanSnapshot,
    Revision,
    TicketState,
    plan_changes_of,
    review_due,
    revise,
)
from sdd_core.ports import Conflict
from sdd_core.tickets import draft_of, ordered, waits_for_known
from sdd_runtime.engine import Engine

from sdd_factory.admission import TicketAdmission, ticket_scope
from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import TaskRecord, language_rule

# Room a review brief leaves in its workflow's input budget for guidance and memory.
REVIEW_MARGIN = 4000
# The human step of the plan-review workflow where a person decides.
DECIDE = "decide"

type TicketCommand = Callable[[Run, str], object]


class PlanReviews:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        flows: FlowLibrary,
        admission: TicketAdmission,
        log: FlightLog,
    ) -> None:
        self.engine, self.catalog, self.flows = engine, catalog, flows
        self.admission, self.log = admission, log

    # Reading the plan -------------------------------------------------------------

    def snapshot(self, parent: str) -> tuple[PlanSnapshot, dict[str, str]]:
        """The approved plan of `parent` with each ticket's state, and ticket -> run ids."""
        drafts = ordered(draft_of(item) for item in self.admission.breakdown(parent))
        runs = self.admission.ticket_runs(parent, drafts)
        states: dict[str, TicketState] = {}
        for draft in drafts:
            try:
                run = self.engine.store.get(runs[draft.id])
            except KeyError:
                continue
            flow = self.engine.store.workflow(run.workflow_digest)
            states[draft.id] = TicketState(
                run.id,
                run.status,
                not discardable(run),
                run.reason,
                run.active is not None,
                run.step,
                tuple(s.id + "*" * s.required for s in flow.steps if s.kind != "finish"),
            )
        return PlanSnapshot(drafts, states, self.admission.documents.specification(parent)), runs

    def reviews_of(self, parent: str) -> dict[str, TaskRecord]:
        return {key: item for key, item in self.catalog.tasks().items() if item.reviews == parent}

    # Starting a review --------------------------------------------------------------

    def request(self, parent: str, trigger: str, detail: str) -> str | None:
        """Start one review of `parent`'s plan for `trigger`; None when not due.
        A closed plan is not reviewed: its rows were delivered or planned again."""
        if self.catalog.task(parent).closed:
            return None
        reviews = self.reviews_of(parent)
        if any(item.trigger == trigger for item in reviews.values()):
            return None  # this very event was reviewed already
        unfinished = any(self.engine.store.get(key).status != "accepted" for key in reviews)
        if not review_due(unfinished, len(reviews)):
            return None
        snapshot, _ = self.snapshot(parent)
        if not snapshot.drafts:
            return None
        feature = self.catalog.task(parent)
        definition = self.flows.ensure("plan-review", feature.project, feature.language)
        rule = language_rule(feature.language)
        budget = self.engine.store.workflow(definition).max_input_chars - REVIEW_MARGIN - len(rule)
        brief = snapshot.brief(detail, self.flows.agents(feature.project), budget)
        with self.engine.store.unit() as db:
            root = Path(db.location(parent).workspace)
        identifier = f"{parent[:80]}-review-{len(reviews) + 1}"
        run = self.engine.create(identifier, definition, root, rule + brief, None, time.time())
        self.catalog.save_task(
            run.id,
            TaskRecord(
                project=feature.project,
                title=f"{feature.title or parent} · plan review",
                language=feature.language,
                plan=feature.plan,
                intent="plan-review",
                reviews=parent,
                trigger=trigger,
            ),
        )
        self.admission.bind(run.id)
        resume = digest(f"{identifier}:resume")[:64]
        self.engine.command(run.id, "resume", resume, run.version, time.time())
        self.log.record("plan_review_requested", run=run.id, plan=parent, trigger=trigger)
        return run.id

    def blocked(self, run: Run) -> str | None:
        """A ticket its own agent blocked, or whose repair loop ran out, asks its plan
        for a review. The ticket's line in the brief carries its reason."""
        item = self.catalog.task(run.id)
        trigger = self._review_trigger(run)
        if item.kind != "ticket" or not item.parent or trigger is None:
            return None
        kind, detail = trigger
        key = f"{kind}:{run.id}:{run.previous_attempt}"
        return self.request(item.parent, key, f"ticket {run.id} {detail}")

    def sweep(self) -> list[str]:
        """Reviews for tickets that stopped while nobody watched (a restart)."""
        with self.engine.store.unit() as db:
            blocked = [run for run in db.runs() if run.status == "blocked"]
        return [started for run in blocked if (started := self.blocked(run))]

    def _review_trigger(self, run: Run) -> tuple[str, str] | None:
        """Why a stopped ticket needs its plan reviewed: its agent blocked it, or its
        repair loop exhausted a step's visits. Engine or operator blocks never do."""
        if run.status != "blocked" or run.previous_attempt is None:
            return None
        if run.cause == "visit_limit":
            return "limit", "exhausted its repair loop"
        if run.cause != "blocked":
            return None
        with self.engine.store.unit() as db:
            results = db.results(run.id)
        agent = any(
            result.attempt_id == run.previous_attempt and result.outcome == "blocked"
            for result in results
        )
        return ("blocked", "was blocked by its agent") if agent else None

    # Deciding -------------------------------------------------------------------------

    def answer(self, doc: dict[str, Json]) -> dict[str, object]:
        """Answer a task. An approved review is checked before and applied after the
        answer; an approved breakdown asks for a review of the fresh plan."""
        run_id = text(doc.get("id"), "id")
        item = self.catalog.task(run_id)
        approved = text(doc.get("outcome"), "outcome") == "approved"
        if item.reviews and approved:
            self.revision(run_id)  # refused here, while the review still awaits the answer
        answered = self.admission.answer(doc)
        if item.reviews and approved:
            self.apply(run_id)
        elif approved and answered.get("admitted"):
            self.request(run_id, f"breakdown:{run_id}", "a fresh breakdown was approved")
        return answered

    def proposal(self, review: str) -> list[Json]:
        """The changes a review proposes, as its agent wrote them (with reasons)."""
        try:
            return sequence(object_json(self._changes_data(review)).get("plan_changes", []))
        except ValueError:
            return []

    def revision(self, review: str) -> Revision:
        parent = self.catalog.task(review).reviews
        snapshot, _ = self.snapshot(parent)
        changes = plan_changes_of(self._changes_data(review))
        revision = revise(snapshot, changes, self.admission.scope_of(parent))
        waits_for_known(revision.create + revision.update, self.admission.known_tickets(parent))
        return revision

    def _changes_data(self, review: str) -> str:
        done = (r for r in self.engine.outputs(review, "plan_changes") if r.outcome == "done")
        found = next(done, None)
        if found is None:
            raise ValueError("The review proposed no changes")
        return found.data

    # Applying ---------------------------------------------------------------------------

    def apply(self, review: str) -> list[str]:
        """Apply an approved review's revision; every step is safe to run again."""
        if "plan_changes" in self.catalog.artifacts(review):
            return []
        parent = self.catalog.task(review).reviews
        revision = self.revision(review)
        _, runs = self.snapshot(parent)
        ids = {**runs, **self.admission.ticket_runs(parent, revision.create)}
        briefed = revision.create + revision.update
        definitions = self.admission.ticket_definitions(parent, briefed)
        contexts = self.admission.ticket_contexts(parent, briefed, definitions)
        created = [
            ids[draft.id]
            for draft in revision.create
            if self.admission.admit_ticket(
                parent, draft, ids, definitions[draft.id], contexts[draft.id]
            )
        ]
        with self.engine.store.unit() as db:
            root = db.location(parent).workspace
        for draft in revision.update:
            claim = self.engine.workspace.claim(root, ticket_scope(Path(root), draft.paths))
            prerequisites = tuple(ids[p] for p in draft.depends_on)
            rewrite = partial(self._rewrite, contexts[draft.id], claim, prerequisites)
            self._on_ticket(review, ids[draft.id], "revise", rewrite)
            # A rewritten ticket runs the flow of the repository it now changes.
            self._on_ticket(
                review, ids[draft.id], "reflow", partial(self._rebind, definitions[draft.id])
            )
        for reflow in revision.reflows:
            change = partial(self._change_flow, reflow.changes)
            self._on_ticket(review, ids[reflow.ticket], "change-flow", change)
        discarded = self._discard(tuple(ids[t] for t in revision.discard))
        for guide in revision.guidance:
            self._on_ticket(review, ids[guide.ticket], "guide", partial(self._advise, guide.text))
            if guide.retry:
                self._on_ticket(review, ids[guide.ticket], "retry", self._command("retry"))
        for ticket in revision.holds:
            self._on_ticket(review, ids[ticket], "hold", self._command("pause"))
        self.admission.save_breakdown(
            parent, revision.drafts, {draft.id: ids[draft.id] for draft in revision.drafts}
        )
        self.catalog.save_artifact(
            Artifact(review, "plan_changes", "\n".join(revision.summary), encode(revision))
        )
        self.log.record(
            "plan_revised",
            run=parent,
            review=review,
            created=list[Json](created),
            discarded=list[Json](discarded),
            changes=list[Json](revision.summary),
        )
        return created

    def reapply(self) -> list[str]:
        """Finish applying reviews an interrupted approval left half done."""
        applied: list[str] = []
        for review, item in self.catalog.tasks().items():
            if not item.reviews or "plan_changes" in self.catalog.artifacts(review):
                continue
            if self._decision(review) != "approved":
                continue
            try:
                self.apply(review)
                applied.append(review)
            except (ValueError, KeyError, Conflict) as error:
                self.log.record("plan_review_failed", "error", run=review, error=str(error))
        return applied

    def _decision(self, review: str) -> str:
        with self.engine.store.unit() as db:
            found = db.step_results(review)
        outcome = next(
            (object_json(doc).get("outcome") for step, doc in found if step == DECIDE), ""
        )
        return outcome if isinstance(outcome, str) else ""

    def _discard(self, runs: tuple[str, ...]) -> tuple[str, ...]:
        """Discard the runs still there: a repeated apply finds them gone already."""
        present = []
        for run in runs:
            try:
                self.engine.store.get(run)
                present.append(run)
            except KeyError:
                continue
        return self.engine.store.discard(tuple(present)) if present else ()

    def _on_ticket(self, review: str, run_id: str, verb: str, action: TicketCommand) -> None:
        """One command on a ticket's run, keyed by review, run and verb so it runs once."""
        run = self.engine.store.get(run_id)
        try:
            action(run, digest(f"{review}:{run_id}:{verb}")[:64])
        except Conflict:
            pass  # recorded before under this request id: applied already

    def _rewrite(
        self, brief: str, claim: str, prerequisites: tuple[str, ...], run: Run, key: str
    ) -> Run:
        return self.engine.revise(
            run.id, brief, claim, prerequisites, key, run.version, time.time()
        )

    def _rebind(self, definition: str, run: Run, key: str) -> Run:
        if run.workflow_digest == definition:
            return run
        return self.engine.migrate(run.id, definition, {}, key, run.version, time.time())

    def _change_flow(self, changes: tuple[FlowChange, ...], run: Run, key: str) -> Run:
        return self.engine.change_flow(run.id, changes, key, run.version, time.time())

    def _advise(self, words: str, run: Run, key: str) -> Run:
        return self.engine.message(run.id, words, key, run.version, time.time())

    def _command(self, command: str) -> TicketCommand:
        return lambda run, key: self.engine.command(run.id, command, key, run.version, time.time())
