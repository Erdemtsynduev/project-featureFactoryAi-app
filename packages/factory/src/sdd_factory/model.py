"""The factory's vocabulary, typed: what the operator creates, approves and runs.

- A **feature** is the input: its specification (a PRD) is written, broken into
  tickets and approved once; its tickets then run. A plan is one feature's source.
- A **ticket** is one vertical slice of a feature: implementation, checks,
  independent review, merge.
- A **task** is a single run of a whole workflow without child tickets.

Work is a tree of any depth: any run whose workflow declares tickets and gets them
approved becomes a parent. A parent's own run finishing means its planning is
done; the parent is delivered when all its children are, or when the operator
closes it early, which detaches its unfinished children (`origin` remembers where
they came from).

Records are application metadata beside the engine's runs, versioned by replacement.
Earlier spellings are rewritten once by the store's data migrations.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Literal

from sdd_core.models import HELD_NEEDS, Json
from sdd_core.tickets import needs_of

type Kind = Literal["feature", "ticket", "task"]
KINDS: tuple[Kind, ...] = ("feature", "ticket", "task")

# Intents offered when creating work, in the order the dialog shows them, with the
# template each one runs.
INTENTS = ("feature", "main-flow", "ticket")
INTENT_KIND: dict[str, Kind] = {"feature": "feature", "main-flow": "task", "ticket": "ticket"}

LANGUAGES = {"ru": "Russian", "en": "English"}

# Claim of a run that only plans and owns no folder of the workspace (a review, a
# tracker item): a path nothing writes, so it never waits for tickets or holds them up.
PLANNING_SCOPE = ".sdd-planning"


@dataclass(frozen=True)
class TaskRecord:
    project: str = ""
    title: str = ""
    kind: Kind = "task"
    language: str = "ru"
    parent: str = ""  # the run a ticket was cut from (a feature or another ticket)
    plan: str = ""  # the plan a feature (and its tickets) came from
    rows: tuple[str, ...] = ()  # plan rows a feature covers
    source: str = ""  # the source document of a feature, workspace-relative
    intent: str = ""
    closed: bool = False  # closed by the operator before all its children were done
    origin: str = ""  # the parent a ticket was detached from when that parent closed
    link: str = ""  # the item's reference in the project's tracker, once mirrored there
    reviews: str = ""  # the feature whose ticket plan this task reviews
    trigger: str = ""  # why a review runs; one review per trigger (a block, a breakdown)
    superseded: str = ""  # the feature that plans this per-row requirement's row now

    @classmethod
    def load(cls, document: dict[str, Json]) -> "TaskRecord":
        raw = str(document.get("kind", "task"))
        spelled = raw
        kind: Kind = next((known for known in KINDS if known == spelled), "task")
        rows = document.get("rows")
        return cls(
            project=str(document.get("project", "")),
            title=str(document.get("title", "")),
            kind=kind,
            language=str(document.get("language", "ru")),
            parent=str(document.get("parent", "")),
            plan=str(document.get("plan", "")),
            rows=tuple(str(row) for row in rows) if isinstance(rows, list) else (),
            source=str(document.get("source", "")),
            intent=str(document.get("intent", "")),
            closed=document.get("closed") is True,
            origin=str(document.get("origin", "")),
            link=str(document.get("link", "")),
            reviews=str(document.get("reviews", "")),
            trigger=str(document.get("trigger", "")),
            superseded=str(document.get("superseded", "")),
        )

    def document(self) -> dict[str, Json]:
        """The stored and served form; empty optional fields are omitted."""
        found: dict[str, Json] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, tuple):
                value = list[Json](value)
            if value or item.name in ("project", "title", "kind", "language"):
                found[item.name] = value
        return found

    def changed(self, **changes: object) -> "TaskRecord":
        return replace(self, **changes)  # type: ignore[arg-type]


def language_rule(language: str) -> str:
    """The first line of every brief: which language user-facing text uses."""
    return (
        f"Response language: {LANGUAGES.get(language, 'Russian')}. Write user-facing questions,"
        " summaries and explanations in this language; keep protocol keys in English.\n"
    )


def ticket_places(breakdown: Json) -> dict[str, dict[str, Json]]:
    """Where each admitted ticket stands in its parent's plan, by run id.

    `breakdown` is the data of a parent's tickets artifact: the approved drafts with
    the run each became. A ticket's wave is 1 plus the latest wave it waits for, so
    wave 1 can start at once and each later wave follows the one before; `after`
    names the tickets it waits for, `needs` what else it needs and `hitl` marks a
    ticket a person must act on (a decision or an asset), so it never starts unasked.
    """
    drafts = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list)
        else []
    )
    needs: dict[str, list[str]] = {}
    for item in drafts:
        listed = item.get("depends_on")
        needs[str(item.get("id"))] = [str(x) for x in listed] if isinstance(listed, list) else []
    waves: dict[str, int] = {}

    def wave(key: str, seen: frozenset[str]) -> int:
        if key not in waves:
            earlier = [d for d in needs.get(key, []) if d in needs and d not in seen]
            waves[key] = 1 + max((wave(d, seen | {key}) for d in earlier), default=0)
        return waves[key]

    places: dict[str, dict[str, Json]] = {}
    for item in drafts:
        key = str(item.get("id"))
        run = item.get("run")
        if not isinstance(run, str):
            continue
        wanted = needs_of(item)
        places[run] = {
            "key": key,
            "wave": wave(key, frozenset()),
            "after": list[Json](needs.get(key, [])),
            "needs": list[Json](wanted),
            "hitl": any(need in HELD_NEEDS for need in wanted),
        }
    return places


def unfinished(
    identifier: str, records: Mapping[str, TaskRecord], status: Mapping[str, str]
) -> list[str]:
    """Children of `identifier` not delivered yet: a child is delivered when its run is
    accepted and so is everything below it, unless that part was closed."""
    below: dict[str, list[str]] = {}
    for key, item in records.items():
        below.setdefault(item.parent, []).append(key)

    def finished(key: str, seen: frozenset[str]) -> bool:
        if status.get(key) != "accepted" or key in seen:
            return status.get(key) == "accepted"
        return records[key].closed or all(
            finished(child, seen | {key}) for child in below.get(key, ())
        )

    return [key for key in below.get(identifier, ()) if not finished(key, frozenset())]
