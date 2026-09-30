"""The factory's vocabulary, typed: what the operator creates, approves and runs.

- A **draft** is the intake: an idea, a plan file or a tracker item, imported once.
  The lead cuts it into features and a person approves the cut.
- A **feature** gets its specification (a PRD), is broken into tickets and approved
  once; its tickets then run.
- A **ticket** is one vertical slice of a feature: implementation, checks,
  independent review, merge.
- A **task** is a single run of a whole workflow without child tickets.

Work is a tree of any depth: any run whose workflow declares a breakdown and gets it
approved becomes a parent (`Children` says what its children are). A parent's own run finishing means its planning is
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

type Kind = Literal["draft", "feature", "ticket", "task"]
KINDS: tuple[Kind, ...] = ("draft", "feature", "ticket", "task")


# Work that only plans: it changes no repository and owns no folder of the workspace.
PLANNING_KINDS: tuple[Kind, ...] = ("draft", "feature")


@dataclass(frozen=True)
class Children:
    """What a parent's approved breakdown becomes."""

    product: str  # the result key the drafts are declared under, and their artifact
    kind: Kind  # what each child is; its flow template has the same name

    @property
    def planning(self) -> bool:
        return self.kind in PLANNING_KINDS


TICKETS = Children("tickets", "ticket")
# One row per kind of parent whose children are not tickets.
CHILDREN: dict[Kind, Children] = {"draft": Children("features", "feature")}
# The artifact kinds a breakdown is stored under.
BREAKDOWNS = tuple(dict.fromkeys(item.product for item in (TICKETS, *CHILDREN.values())))


def children_of(kind: Kind) -> Children:
    return CHILDREN.get(kind, TICKETS)


# Intents offered when creating work, in the order the dialog shows them, with the
# template each one runs.
INTENTS = ("draft", "feature", "main-flow", "ticket")
INTENT_KIND: dict[str, Kind] = {
    "draft": "draft",
    "feature": "feature",
    "main-flow": "task",
    "ticket": "ticket",
}

LANGUAGES = {"ru": "Russian", "en": "English"}

# Claim of a run that only plans and owns no folder of the workspace (a draft, a
# feature, a review): a path nothing writes, so it never waits for tickets or holds
# them up, and edits elsewhere never invalidate it.
PLANNING_SCOPE = ".sdd-planning"


@dataclass(frozen=True)
class TaskRecord:
    project: str = ""
    title: str = ""
    kind: Kind = "task"
    language: str = "ru"
    parent: str = ""  # the run this work was cut from (a draft, a feature, a ticket)
    rows: tuple[str, ...] = ()  # source rows a draft or a feature covers
    source: str = ""  # where the work came from (a file, a tracker item); children keep it
    labels: tuple[str, ...] = ()  # short tags people filter and start work by
    intent: str = ""
    closed: bool = False  # closed by the operator before all its children were done
    origin: str = ""  # the parent a ticket was detached from when that parent closed
    link: str = ""  # the item's reference in the project's tracker, once mirrored there
    reviews: str = ""  # the feature whose ticket plan this task reviews
    trigger: str = ""  # why a review runs; one review per trigger (a block, a breakdown)
    superseded: str = ""  # the feature that plans this work's scope now

    @classmethod
    def load(cls, document: dict[str, Json]) -> "TaskRecord":
        raw = str(document.get("kind", "task"))
        spelled = raw
        kind: Kind = next((known for known in KINDS if known == spelled), "task")
        rows, labels = document.get("rows"), document.get("labels")
        return cls(
            project=str(document.get("project", "")),
            title=str(document.get("title", "")),
            kind=kind,
            language=str(document.get("language", "ru")),
            parent=str(document.get("parent", "")),
            rows=tuple(str(row) for row in rows) if isinstance(rows, list) else (),
            source=str(document.get("source", "")),
            labels=tuple(str(label) for label in labels) if isinstance(labels, list) else (),
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
