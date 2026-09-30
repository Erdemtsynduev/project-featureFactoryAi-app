"""Plan review: corrections to an approved ticket breakdown, decided purely.

A reviewing agent proposes changes; a person approves them; the application then
applies the `Revision` computed here. Structural changes (revise, merge, split,
cancel) touch only tickets that never started, so no started work is rewritten;
needs and guidance may address any unfinished ticket. The revised plan must still
be a valid breakdown, and tickets that depended on a removed ticket are rewired to
what replaced it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from sdd_core.editor import FlowChange, flow_changes_of
from sdd_core.models import PLAN_CHANGE_KINDS, PlanChangeKind, Status, TicketNeed
from sdd_core.tickets import (
    ScopeOf,
    TicketDraft,
    draft_of,
    needs_of,
    one_repository,
    ordered,
    strings,
)
from sdd_core.wire import Json, flag, mapping, object_json, sequence, text

# Reviews one plan may take; a plan that needs more needs a person, not more reviews.
MAX_REVIEWS = 5


@dataclass(frozen=True)
class Revise:
    """Rewrite a never-started ticket (same id)."""

    draft: TicketDraft


@dataclass(frozen=True)
class Merge:
    """Fold never-started tickets into one; the draft keeps one of their ids."""

    sources: tuple[str, ...]
    draft: TicketDraft


@dataclass(frozen=True)
class Split:
    """Replace a never-started ticket by new, smaller tickets."""

    ticket: str
    drafts: tuple[TicketDraft, ...]


@dataclass(frozen=True)
class Cancel:
    """Drop a never-started ticket; its dependents inherit its prerequisites."""

    ticket: str


@dataclass(frozen=True)
class Need:
    """Declare what an unfinished ticket needs; a person's need holds it."""

    ticket: str
    needs: tuple[TicketNeed, ...]


@dataclass(frozen=True)
class Guide:
    """Tell an unfinished ticket's next attempt what to do differently."""

    ticket: str
    text: str
    retry: bool = False


@dataclass(frozen=True)
class Reflow:
    """Change the flow of an unfinished, idle ticket (skip a step, another profile)."""

    ticket: str
    changes: tuple[FlowChange, ...]


type PlanChange = Revise | Merge | Split | Cancel | Need | Guide | Reflow


def _drafts(item: Mapping[str, Json]) -> tuple[TicketDraft, ...]:
    return tuple(draft_of(raw) for raw in sequence(item.get("drafts", [])))


def _one(item: Mapping[str, Json]) -> TicketDraft:
    drafts = _drafts(item)
    if len(drafts) != 1:
        raise ValueError("This change needs exactly one draft")
    return drafts[0]


def _ticket(item: Mapping[str, Json]) -> str:
    identifier = text(item.get("ticket"), "ticket").strip()
    if not identifier:
        raise ValueError("This change needs a ticket")
    return identifier


READERS: dict[PlanChangeKind, Callable[[Mapping[str, Json]], PlanChange]] = {
    "revise": lambda item: Revise(_one(item)),
    "merge": lambda item: Merge(strings(item.get("tickets"), "ticket"), _one(item)),
    "split": lambda item: Split(_ticket(item), _drafts(item)),
    "cancel": lambda item: Cancel(_ticket(item)),
    "need": lambda item: Need(_ticket(item), needs_of(dict(item))),
    "guide": lambda item: Guide(
        _ticket(item),
        text(item.get("text", ""), "text").strip(),
        flag(item.get("retry", False), "retry"),
    ),
    "reflow": lambda item: Reflow(_ticket(item), flow_changes_of(item.get("flow", []))),
}


def plan_changes_of(data: str) -> tuple[PlanChange, ...]:
    """The changes a review step declared in its result data."""
    changes: list[PlanChange] = []
    for raw in sequence(object_json(data or "{}").get("plan_changes", [])):
        item = mapping(raw)
        kind = text(item.get("kind"), "kind")
        if kind not in PLAN_CHANGE_KINDS:
            raise ValueError(f"Unknown plan change: {kind}")
        changes.append(READERS[kind](item))
    return tuple(changes)


@dataclass(frozen=True)
class TicketState:
    """Where a ticket's run stands, as the review sees it."""

    run: str
    status: Status
    started: bool
    reason: str = ""
    live: bool = False  # an attempt is running now
    step: str = ""  # where the ticket is in its flow
    flow: tuple[str, ...] = ()  # its flow's steps in order, required ones marked `*`

    @property
    def finished(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True)
class PlanSnapshot:
    """An approved breakdown and the state of each ticket's run."""

    drafts: tuple[TicketDraft, ...]
    states: Mapping[str, TicketState]
    specification: str = ""

    def brief(self, trigger: str, capabilities: str, limit: int) -> str:
        """The review packet: why it runs, what agents can do, every ticket and its state."""
        lines = [f"Plan review requested: {trigger}", "", "Ticket agents:", capabilities, ""]
        lines.append("Tickets (id · state · needs · depends on · owned paths):")
        for draft in self.drafts:
            state = self.states.get(draft.id)
            where = (
                "not admitted"
                if state is None
                else state.status + ("" if state.started else " (never started)")
            )
            lines.append(
                f"- {draft.id}: {draft.title} · {where} · needs {', '.join(draft.needs) or '-'}"
                f" · after {', '.join(draft.depends_on) or '-'} · {', '.join(draft.paths) or '-'}"
            )
            if draft.goal:
                lines.append(f"  Goal: {draft.goal}")
            lines.extend(f"  AC: {item}" for item in draft.acceptance)
            if state is not None and state.flow:
                lines.append(f"  Flow: {' > '.join(state.flow)} (at {state.step})")
            if state is not None and state.reason:
                lines.append(f"  Blocked: {state.reason}")
        plan = "\n".join(lines)
        if len(plan) > limit:
            raise ValueError("The plan exceeds the review's context budget")
        room = limit - len(plan) - 40
        specification = self.specification[: max(0, room)]
        if len(specification) < len(self.specification):
            specification = specification.rsplit("\n", 1)[0] + "\n[Specification shortened]"
        return plan + ("\n\nApproved specification:\n" + specification if specification else "")


@dataclass(frozen=True)
class Revision:
    """What approving a review changes, ready for the application to apply."""

    drafts: tuple[TicketDraft, ...]
    create: tuple[TicketDraft, ...] = ()
    update: tuple[TicketDraft, ...] = ()
    discard: tuple[str, ...] = ()
    holds: tuple[str, ...] = ()
    guidance: tuple[Guide, ...] = ()
    summary: tuple[str, ...] = ()
    reflows: tuple[Reflow, ...] = ()


def revise(
    snapshot: PlanSnapshot,
    changes: tuple[PlanChange, ...],
    scope_of: ScopeOf = lambda draft: (),
) -> Revision:
    """Apply `changes` to the plan, or refuse them with the first broken rule.

    `scope_of` names the repositories a draft changes: every new or rewritten ticket
    must change one repository (see `tickets.one_repository`).
    """
    plan = {draft.id: draft for draft in snapshot.drafts}
    known = set(plan)
    touched: set[str] = set()
    replaced: dict[str, tuple[str, ...]] = {}
    created: dict[str, TicketDraft] = {}
    holds: list[str] = []
    guidance: list[Guide] = []
    reflows: list[Reflow] = []
    summary: list[str] = []

    def existing(identifier: str) -> TicketDraft:
        if identifier not in plan:
            raise ValueError(f"Unknown ticket: {identifier}")
        return plan[identifier]

    def unstarted(identifier: str) -> None:
        existing(identifier)
        state = snapshot.states.get(identifier)
        if state is not None and state.started:
            raise ValueError(f"Ticket {identifier} has started; only needs and guidance apply")
        if identifier in touched:
            raise ValueError(f"Ticket {identifier} is changed twice")
        touched.add(identifier)

    def unfinished(identifier: str) -> None:
        existing(identifier)
        state = snapshot.states.get(identifier)
        if state is not None and state.finished:
            raise ValueError(f"Ticket {identifier} is finished")

    def fresh(draft: TicketDraft) -> None:
        if draft.id in known:
            raise ValueError(f"Ticket id already used: {draft.id}")
        known.add(draft.id)
        created[draft.id] = draft

    for change in changes:
        match change:
            case Revise(draft):
                unstarted(draft.id)
                plan[draft.id] = draft
                summary.append(f"revise {draft.id}")
            case Merge(sources, draft):
                if len(sources) < 2 or draft.id not in sources:
                    raise ValueError("A merge folds two or more tickets into one of them")
                for source in sources:
                    unstarted(source)
                for source in sources:
                    if source != draft.id:
                        replaced[source] = (draft.id,)
                        del plan[source]
                plan[draft.id] = draft
                summary.append(f"merge {', '.join(sources)} into {draft.id}")
            case Split(ticket, drafts):
                if len(drafts) < 2:
                    raise ValueError("A split needs two or more new tickets")
                unstarted(ticket)
                for draft in drafts:
                    fresh(draft)
                    plan[draft.id] = draft
                replaced[ticket] = tuple(draft.id for draft in drafts)
                del plan[ticket]
                summary.append(f"split {ticket} into {', '.join(d.id for d in drafts)}")
            case Cancel(ticket):
                unstarted(ticket)
                replaced[ticket] = plan[ticket].depends_on
                del plan[ticket]
                summary.append(f"cancel {ticket}")
            case Need(ticket, needs):
                unfinished(ticket)
                plan[ticket] = replace(plan[ticket], needs=needs)
                if plan[ticket].held:
                    holds.append(ticket)
                summary.append(f"{ticket} needs {', '.join(needs) or 'nothing more'}")
            case Guide(ticket, words, _):
                unfinished(ticket)
                if not words:
                    raise ValueError("Guidance needs text")
                guidance.append(change)
                summary.append(f"guide {ticket}")
            case Reflow(ticket, flow):
                unfinished(ticket)
                if (snapshot.states.get(ticket) or _UNSTARTED).live:
                    raise ValueError(f"Ticket {ticket} is running; change its flow after")
                if not flow:
                    raise ValueError("A flow change needs at least one change")
                reflows.append(change)
                summary.append(f"reflow {ticket}")

    rewired = {
        identifier: replace(draft, depends_on=_rewire(draft.depends_on, replaced, identifier))
        for identifier, draft in plan.items()
    }
    drafts = ordered(rewired.values())
    before = {draft.id: draft for draft in snapshot.drafts}
    update = tuple(
        draft
        for draft in drafts
        if draft.id in before
        and draft != before[draft.id]
        and not (snapshot.states.get(draft.id) or _UNSTARTED).started
    )
    create = tuple(draft for draft in drafts if draft.id in created)
    one_repository(create + update, scope_of)
    return Revision(
        drafts,
        create,
        update,
        tuple(sorted(replaced)),
        tuple(holds),
        tuple(guidance),
        tuple(summary),
        tuple(reflows),
    )


_UNSTARTED = TicketState("", "ready", False)


def _rewire(
    depends_on: tuple[str, ...], replaced: Mapping[str, tuple[str, ...]], owner: str
) -> tuple[str, ...]:
    """Prerequisites with every removed ticket replaced by what took its place."""
    result: list[str] = []
    pending = list(depends_on)
    seen: set[str] = set()
    while pending:
        identifier = pending.pop(0)
        if identifier in seen:
            continue
        seen.add(identifier)
        if identifier in replaced:
            pending[:0] = replaced[identifier]
        elif identifier != owner:
            result.append(identifier)
    return tuple(dict.fromkeys(result))


def review_due(unfinished_review: bool, reviews: int) -> bool:
    """Whether a plan may get another review now."""
    return not unfinished_review and reviews < MAX_REVIEWS
