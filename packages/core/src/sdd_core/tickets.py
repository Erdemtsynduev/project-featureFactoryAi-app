"""Ticket drafts: bounded slices a planning step declares in its result data.

The application turns an approved breakdown into child runs; each draft carries
everything a fresh ticket agent needs, without the planner's transcript.
"""

from dataclasses import dataclass

from sdd_core.graph import dependency_layers
from sdd_core.wire import Json, mapping, object_json, sequence, text


def strings(raw: Json, label: str) -> tuple[str, ...]:
    """Non-empty, stripped texts of an optional JSON list."""
    values = (text(item, label).strip() for item in sequence(raw if raw is not None else []))
    return tuple(value for value in values if value)


@dataclass(frozen=True)
class TicketDraft:
    id: str
    title: str
    goal: str = ""
    acceptance: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def context(self, specification: str, limit: int | None = None) -> str:
        """Everything a fresh ticket agent needs, without the planner's transcript.

        Within `limit` characters the ticket itself is kept whole; a specification
        that does not fit is cut at a line and says so, never silently.
        """
        lines = [f"Ticket {self.id}: {self.title}"]
        if self.goal:
            lines.append("Goal: " + self.goal)
        if self.acceptance:
            lines.append("Acceptance criteria:")
            lines.extend("- " + item for item in self.acceptance)
        if self.paths:
            lines.append("Owned paths: " + ", ".join(self.paths))
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


def tickets_of(data: str) -> tuple[TicketDraft, ...]:
    """Ticket drafts a planning step declared in its result data, in dependency order."""
    drafts: dict[str, TicketDraft] = {}
    for raw in sequence(object_json(data or "{}").get("tickets", [])):
        item = mapping(raw)
        draft = TicketDraft(
            text(item.get("id"), "ticket id").strip(),
            text(item.get("title"), "ticket title").strip(),
            text(item.get("goal", ""), "goal").strip(),
            strings(item.get("acceptance"), "acceptance"),
            strings(item.get("depends_on"), "dependency"),
            strings(item.get("paths"), "path"),
        )
        if not draft.id or not draft.title:
            raise ValueError("Ticket needs an id and a title")
        if draft.id in drafts:
            raise ValueError("Duplicate ticket id")
        drafts[draft.id] = draft
    layers = dependency_layers(
        {draft.id: draft.depends_on for draft in drafts.values()},
        "Ticket dependencies are cyclic or unknown",
    )
    return tuple(drafts[identifier] for layer in layers for identifier in layer)
