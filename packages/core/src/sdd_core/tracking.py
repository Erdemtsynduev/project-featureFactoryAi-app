"""Work outside the engine: where features come from and where their progress is shown.

A **work source** supplies work items: numbered Markdown plans in the project, the
projects of a Linear team, the milestones of a repository. Each item becomes one
feature over its open rows. A **tracker** is a work source that also mirrors back
what the factory decided and did: the approved specification, the tickets it
admitted and the state of each item. Adapters are trusted installed code chosen by
explicit project configuration; they never run from workflow text.

Nothing here does IO. A publication is data recorded before delivery (an outbox),
so a tracker being down never loses or reorders what people should see.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from sdd_core.codec import canonical, decode, digest, encode, object_json

OPEN_MARKS = (" ", "~")

# What a person sees about an item in the tracker, derived from the engine's state.
type MirrorState = Literal["queued", "working", "needs_person", "blocked", "done"]
MIRROR_STATES: tuple[MirrorState, ...] = ("queued", "working", "needs_person", "blocked", "done")


@dataclass(frozen=True)
class WorkRow:
    """One requirement of a work item. `mark` is its author's status: `x` done,
    `~` partially done, a space for open and `-` rejected."""

    id: str
    mark: str
    text: str
    detail: tuple[str, ...] = ()
    link: str = ""  # the row's own tracker reference, when it has one

    @property
    def open(self) -> bool:
        return self.mark in OPEN_MARKS

    @property
    def title(self) -> str:
        return f"{self.id} — {self.text}" if self.text else self.id


@dataclass(frozen=True)
class WorkItem:
    """A source of one feature: its scope is the open and partial rows.

    `key` is stable across reads and names the feature (`feature_<key>`); `body`
    holds the goals, rules and findings people wrote. `path` is set when the item
    is a file in the workspace, `link` and `url` when it lives in a tracker.
    """

    key: str
    title: str
    body: str
    rows: tuple[WorkRow, ...]
    path: str = ""
    link: str = ""
    url: str = ""

    def counts(self) -> dict[str, int]:
        return {
            "requirements": len(self.rows),
            "accepted": sum(row.mark == "x" for row in self.rows),
            "partial": sum(row.mark == "~" for row in self.rows),
            "open": sum(row.mark == " " for row in self.rows),
            "rejected": sum(row.mark == "-" for row in self.rows),
        }


class WorkSource(Protocol):
    """Reads work items; `workspace` is the project's folder."""

    def items(self, workspace: str) -> list[WorkItem]: ...


@dataclass(frozen=True)
class TicketMirror:
    """An admitted ticket as a tracker shows it (a sub-issue of its feature)."""

    run: str
    key: str
    title: str
    goal: str = ""
    acceptance: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    wave: int = 1
    hitl: bool = False


type UpdateKind = Literal["specification", "tickets", "state"]


@dataclass(frozen=True)
class TrackerUpdate:
    """One thing to show in a tracker about the item `link` (factory run `run`).

    - `specification`: the approved specification in `text`;
    - `tickets`: the admitted tickets, to create as children of the item;
    - `state`: the item's `state`, with the reason in `text`.

    Identical updates share an `id`, so recording one twice publishes it once.
    """

    kind: UpdateKind
    run: str
    link: str
    text: str = ""
    state: str = ""
    tickets: tuple[TicketMirror, ...] = field(default=())

    @property
    def id(self) -> str:
        # The stored document's values in field order; tickets as value lists.
        document = encode(self)
        tickets = [list(ticket.values()) for ticket in document.pop("tickets")]
        return digest(canonical([*document.values(), tickets]))[:32]


@dataclass(frozen=True)
class TrackerReceipt:
    """What delivery created: tracker references of new items, by factory run id."""

    links: tuple[tuple[str, str], ...] = ()


class Tracker(WorkSource, Protocol):
    """A work source that also shows the factory's progress to people.

    `publish` must be safe to repeat for the same update: delivery is retried
    until it succeeds, and a crash may repeat the last delivery.
    """

    id: str

    def publish(self, update: TrackerUpdate) -> TrackerReceipt: ...


def mirror_state(status: str, attention: str) -> MirrorState:
    """The tracker state of a run from its engine status and board attention code.

    A parent's attention comes from its children, so a feature shows the state of
    its delivery rather than of its own finished planning run.
    """
    if attention in ("delivered", "closed") or (status == "accepted" and attention == "accepted"):
        return "done"
    if attention in ("answer", "children_need"):
        return "needs_person"
    if attention in ("blocked", "limits_exhausted", "uncertain", "no_profile"):
        return "blocked"
    if attention in ("working", "delivering") or status == "running":
        return "working"
    return "queued"


def update_json(update: TrackerUpdate) -> str:
    """The stored form of a publication; `update_load` reads it back."""
    return canonical(encode(update))


def update_load(document: str) -> TrackerUpdate:
    return decode(TrackerUpdate, object_json(document))


def receipt_json(receipt: TrackerReceipt) -> str:
    return canonical({"links": [list(pair) for pair in receipt.links]})
