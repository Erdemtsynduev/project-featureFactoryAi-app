"""Task memory shared by every agent of a run, built deterministically from receipts.

Agents do not re-read each other's raw receipts. Each result may carry short
durable `notes` (decisions, owned paths, constraints) in its data; the next
packet receives the de-duplicated notes of the whole run plus a compact handoff
of the latest outcomes. No model summarizes anything: the brief is a pure
function of recorded results, so replaying the same results gives the same brief.

Ticket drafts a planning step declares live in `sdd_core.tickets`.
"""

from dataclasses import dataclass

from sdd_core.codec import object_json, result_load
from sdd_core.tickets import strings

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


def notes(data: str) -> tuple[str, ...]:
    """Notes declared in one result's data, each bounded in length."""
    return tuple(
        note[:NOTE_CHARS] for note in strings(object_json(data or "{}").get("notes"), "note")
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
