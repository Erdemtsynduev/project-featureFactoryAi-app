"""Task memory shared by every agent of a run, built deterministically from receipts.

Agents do not re-read each other's raw receipts. Each result may carry short
durable `notes` (decisions, owned paths, constraints) in its data; the next
packet receives the de-duplicated notes of the whole run plus a compact handoff
of the latest outcomes. No model summarizes anything: the brief is a pure
function of recorded results, so replaying the same results gives the same brief.

A planning step may also declare `tickets`: bounded slices that the application
turns into child runs once a human approves the breakdown.
"""

from dataclasses import dataclass

from sdd_core.codec import mapping, object_json, result_load, sequence, text
from sdd_core.models import Json

# One handoff line keeps the head of a reason; the full receipt stays on disk.
HANDOFF_CHARS = 1600
NOTE_CHARS = 280
MAX_NOTES = 40


@dataclass(frozen=True)
class Brief:
    notes: tuple[str, ...]
    handoff: tuple[str, ...]

    def render(self) -> str:
        parts: list[str] = []
        if self.notes:
            parts.append("Task memory (durable notes from earlier steps):")
            parts.extend("- " + note for note in self.notes)
        if self.handoff:
            parts.append("Handoff (newest first):")
            parts.extend(self.handoff)
        return "\n".join(parts)


def _strings(raw: Json, label: str) -> tuple[str, ...]:
    values = (text(item, label).strip() for item in sequence(raw if raw is not None else []))
    return tuple(value for value in values if value)


def notes(data: str) -> tuple[str, ...]:
    """Notes declared in one result's data, each bounded in length."""
    return tuple(
        note[:NOTE_CHARS] for note in _strings(object_json(data or "{}").get("notes"), "note")
    )


def brief(documents: tuple[str, ...], handoff_results: int = 3) -> Brief:
    """Memory for the next packet from receipts ordered newest first."""
    results = [result_load(document) for document in documents]
    handoff: list[str] = []
    for result in results[:handoff_results]:
        reason = " ".join(result.reason.split())
        if len(reason) > HANDOFF_CHARS:
            reason = reason[:HANDOFF_CHARS] + " …"
        handoff.append(f"- [{result.outcome}] {reason}")
    # Oldest decisions first, so the list reads as the story of the task.
    seen: dict[str, None] = {}
    for result in reversed(results):
        for note in notes(result.data):
            seen.setdefault(note, None)
    return Brief(tuple(seen)[-MAX_NOTES:], tuple(handoff))


def fit(context: str, memory: str, limit: int) -> str:
    """Append memory within the input budget, keeping its newest lines."""
    if not memory:
        return context
    room = limit - len(context) - 1
    if room >= len(memory):
        return context + "\n" + memory
    if room < 200:
        return context
    return context + "\n" + memory[-room:].split("\n", 1)[-1]


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
            _strings(item.get("acceptance"), "acceptance"),
            _strings(item.get("depends_on"), "dependency"),
            _strings(item.get("paths"), "path"),
        )
        if not draft.id or not draft.title:
            raise ValueError("Ticket needs an id and a title")
        if draft.id in drafts:
            raise ValueError("Duplicate ticket id")
        drafts[draft.id] = draft
    ordered: list[TicketDraft] = []
    while len(ordered) < len(drafts):
        done = {draft.id for draft in ordered}
        ready = [d for d in drafts.values() if d.id not in done and set(d.depends_on) <= done]
        if not ready:
            raise ValueError("Ticket dependencies are cyclic or unknown")
        ordered.extend(ready)
    return tuple(ordered)
