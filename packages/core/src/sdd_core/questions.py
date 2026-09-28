"""Structured agent questions and deterministic recommended answers.

An agent that needs a decision returns `questions` in its result data: each has
an id, the question text, 2..6 concrete options and exactly one recommended
option. A human answers by choosing options (or writing text); when the task
allows it, the engine answers with the recommendations instead. Both paths
produce the same answer document, so the next step cannot tell them apart
except through the recorded `auto` flag.
"""

from dataclasses import dataclass

from sdd_core.codec import canonical, mapping, object_json, sequence, text
from sdd_core.models import Json


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    options: tuple[str, ...]
    recommended: str


def questions(facts: str) -> tuple[Question, ...]:
    """Questions declared by the previous result; malformed entries are rejected."""
    document = object_json(facts or "{}")
    found: list[Question] = []
    for raw in sequence(document.get("questions", [])):
        item = mapping(raw)
        options = tuple(
            text(option, "option").strip() for option in sequence(item.get("options", []))
        )
        question = Question(
            text(item.get("id"), "id").strip(),
            text(item.get("question"), "question").strip(),
            options,
            text(item.get("recommended", ""), "recommended").strip(),
        )
        if not question.id or not question.text or len(set(options)) != len(options):
            raise ValueError("Question needs an id, text and distinct options")
        if options and not 2 <= len(options) <= 6:
            raise ValueError("Question offers 2..6 options")
        if question.recommended and question.recommended not in options:
            raise ValueError("Recommended answer must be one of the options")
        found.append(question)
    if len({q.id for q in found}) != len(found):
        raise ValueError("Duplicate question id")
    return tuple(found)


def answer(asked: tuple[Question, ...], choices: dict[str, str], note: str = "") -> tuple[str, str]:
    """Readable answer text plus the answer document stored with the human result."""
    lines = []
    for question in asked:
        choice = choices.get(question.id, "").strip()
        if choice:
            lines.append(f"{question.text}\n→ {choice}")
    if note.strip():
        lines.append(note.strip())
    if not lines:
        raise ValueError("Answer at least one question or write a reply")
    reply = "\n\n".join(lines)
    data: dict[str, Json] = {"answer": reply}
    if choices:
        data["choices"] = {key: value for key, value in sorted(choices.items()) if value}
    return reply, canonical(data)


def recommended(facts: str) -> tuple[str, str] | None:
    """The engine's answer when every question carries a recommendation, else None."""
    asked = questions(facts)
    if not asked or any(not q.recommended for q in asked):
        return None
    reply, data = answer(asked, {q.id: q.recommended for q in asked})
    document = object_json(data)
    document["auto"] = True
    return "Recommended options accepted automatically:\n\n" + reply, canonical(document)
