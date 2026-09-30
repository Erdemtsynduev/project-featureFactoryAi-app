"""Breakdown drafts: bounded slices a planning step declares in its result data.

A feature is broken into tickets, and a draft into features, by the same kind of
step. The application turns an approved breakdown into child runs; each draft
carries everything a fresh agent needs, without the planner's transcript.
"""

import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import cast

from sdd_core.graph import dependency_layers
from sdd_core.models import HELD_NEEDS, TICKET_NEEDS, TicketNeed
from sdd_core.wire import Json, mapping, object_json, sequence, text


def strings(raw: Json, label: str) -> tuple[str, ...]:
    """Non-empty, stripped texts of an optional JSON list."""
    values = (text(item, label).strip() for item in sequence(raw if raw is not None else []))
    return tuple(value for value in values if value)


# A label is a short lowercase tag: letters of any script, digits and hyphens.
LABEL = re.compile(r"[\w-]{1,32}")
MAX_LABELS = 3


def labels_of(raw: Json) -> tuple[str, ...]:
    """Labels as people write them, normalised: lowercase, hyphens for spaces, unique."""
    labels = tuple(
        dict.fromkeys("-".join(label.lower().split()) for label in strings(raw, "label"))
    )
    if invalid := [label for label in labels if not LABEL.fullmatch(label)]:
        raise ValueError(f"Labels are short words without punctuation: {', '.join(invalid)}")
    if len(labels) > MAX_LABELS:
        raise ValueError(f"At most {MAX_LABELS} labels: {', '.join(labels)}")
    return labels


@dataclass(frozen=True)
class TicketDraft:
    """One slice of its parent: a ticket of a feature, or a feature of a draft."""

    id: str
    title: str
    goal: str = ""
    acceptance: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    # What the ticket needs besides an agent run (see `models.TicketNeed`).
    needs: tuple[TicketNeed, ...] = ()
    # Existing work from the same source (run ids, not in this breakdown) it waits for.
    after: tuple[str, ...] = ()
    # Short tags people filter and start work by.
    labels: tuple[str, ...] = ()
    # Ids of the parent's source rows this slice covers.
    covers: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        """A person must act before (or instead of) the ticket's agent."""
        return any(need in HELD_NEEDS for need in self.needs)

    def context(self, specification: str, limit: int | None = None) -> str:
        """Everything a fresh ticket agent needs, without the planner's transcript.

        Within `limit` characters the ticket itself is kept whole; a specification
        that does not fit is cut at a line and says so, never silently.
        """
        lines = [f"{TICKET_HEADER}{self.id}: {self.title}"]
        if self.goal:
            lines.append("Goal: " + self.goal)
        if self.acceptance:
            lines.append("Acceptance criteria:")
            lines.extend("- " + item for item in self.acceptance)
        if self.paths:
            lines.append("Owned paths: " + ", ".join(self.paths))
        if self.needs:
            lines.append("Needs: " + ", ".join(self.needs))
        ticket = "\n".join(lines)
        if limit is not None and len(ticket) > limit:
            raise ValueError(f"Ticket {self.id} exceeds its context budget")
        if not specification:
            return ticket
        heading = "\nApproved specification of the parent requirement:\n"
        room = len(specification) if limit is None else limit - len(ticket) - len(heading)
        if len(specification) <= room:
            return ticket + heading + specification
        # The kept count never has more digits than the total, so this bounds the marker.
        room -= len(_shortened(len(specification), len(specification)))
        if room < 200:
            return ticket + (_shortened(0, len(specification)) if room >= 0 else "")
        cut = specification[:room]
        kept = cut.rsplit("\n", 1)[0] if "\n" in cut else cut
        return ticket + heading + kept + _shortened(len(kept), len(specification))


def _shortened(kept: int, total: int) -> str:
    return (
        f"\n[Specification shortened to fit the ticket budget: {kept} of {total}"
        " characters. The acceptance criteria above govern.]"
    )


TICKET_HEADER = "Ticket "


def ticket_title(context: str) -> str:
    """The title a ticket's brief opens with (see `TicketDraft.context`), else ""."""
    for line in context.splitlines():
        if line.startswith(TICKET_HEADER) and ": " in line:
            return line.split(": ", 1)[1].strip()
    return ""


def needs_of(item: dict[str, Json]) -> tuple[TicketNeed, ...]:
    """A ticket's needs; a breakdown written before `needs` marked people with "HITL"."""
    if "needs" not in item:
        goal = item.get("goal")
        return ("human",) if isinstance(goal, str) and goal.strip().startswith("HITL") else ()
    needs = strings(item.get("needs"), "need")
    unknown = sorted(set(needs) - set(TICKET_NEEDS))
    if unknown:
        raise ValueError(f"Unknown ticket needs: {', '.join(unknown)}")
    return tuple(dict.fromkeys(cast(tuple[TicketNeed, ...], needs)))


def draft_of(raw: Json) -> TicketDraft:
    """One ticket draft from its JSON object; texts are stripped, empty items dropped."""
    item = mapping(raw)
    draft = TicketDraft(
        text(item.get("id"), "ticket id").strip(),
        text(item.get("title"), "ticket title").strip(),
        text(item.get("goal", ""), "goal").strip(),
        strings(item.get("acceptance"), "acceptance"),
        strings(item.get("depends_on"), "dependency"),
        strings(item.get("paths"), "path"),
        needs_of(item),
        strings(item.get("after"), "dependency"),
        labels_of(item.get("labels")),
        strings(item.get("covers"), "row"),
    )
    if not draft.id or not draft.title:
        raise ValueError("Ticket needs an id and a title")
    return draft


def ordered(drafts: Iterable[TicketDraft]) -> tuple[TicketDraft, ...]:
    """Drafts in dependency order; duplicate ids, cycles and unknown ids are refused."""
    by_id: dict[str, TicketDraft] = {}
    for draft in drafts:
        if draft.id in by_id:
            raise ValueError("Duplicate ticket id")
        by_id[draft.id] = draft
    layers = dependency_layers(
        {draft.id: draft.depends_on for draft in by_id.values()},
        "Ticket dependencies are cyclic or unknown",
    )
    return tuple(by_id[identifier] for layer in layers for identifier in layer)


def waits_for_known(drafts: Iterable[TicketDraft], known: Collection[str]) -> None:
    """Every `after` names existing work from the same source; a guess is refused with
    the list."""
    unknown = [
        f"{draft.id}: {', '.join(missing)}"
        for draft in drafts
        if (missing := [run for run in draft.after if run not in known])
    ]
    if unknown:
        raise ValueError(
            "Tickets wait for work that is not a ticket from this source (use the run ids "
            "the brief lists): " + "; ".join(unknown)
        )


type ScopeOf = Callable[[TicketDraft], tuple[str, ...]]


def one_repository(drafts: Iterable[TicketDraft], scope_of: ScopeOf) -> None:
    """Each ticket changes one repository. Work across repositories is a chain of
    tickets (the dependency first), so each can be committed, pinned and verified."""
    spanning = [
        f"{draft.id}: {', '.join(repositories)}"
        for draft in drafts
        if len(repositories := scope_of(draft)) > 1
    ]
    if spanning:
        raise ValueError(
            "Each ticket changes one repository; split these by repository, the dependency "
            "first: " + "; ".join(spanning)
        )


def covers_rows(drafts: Iterable[TicketDraft], rows: Collection[str]) -> None:
    """A breakdown of a parent with source rows covers each row exactly once: nothing
    is dropped, planned twice or invented."""
    covered: dict[str, list[str]] = {}
    for draft in drafts:
        for row in draft.covers:
            covered.setdefault(row, []).append(draft.id)
    problems = [
        *(f"{row} is not a row of this draft" for row in sorted(set(covered) - set(rows))),
        *(f"{row} is covered by no feature" for row in sorted(set(rows) - set(covered))),
        *(
            f"{row} is covered by {', '.join(owners)}"
            for row, owners in sorted(covered.items())
            if len(owners) > 1
        ),
    ]
    if problems:
        raise ValueError("Every open row belongs to exactly one feature: " + "; ".join(problems))


def drafts_of(data: str, product: str) -> tuple[TicketDraft, ...]:
    """The drafts a planning step declared under `product` in its result data, in
    dependency order."""
    return ordered(draft_of(raw) for raw in sequence(object_json(data or "{}").get(product, [])))


def tickets_of(data: str) -> tuple[TicketDraft, ...]:
    return drafts_of(data, "tickets")


def features_of(data: str) -> tuple[TicketDraft, ...]:
    return drafts_of(data, "features")
