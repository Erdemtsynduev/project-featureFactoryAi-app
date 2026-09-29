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
from typing import Literal, Protocol, cast

from sdd_core.codec import canonical, digest, integer, mapping, object_json, sequence, text

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
        return digest(
            canonical(
                [
                    self.kind,
                    self.run,
                    self.link,
                    self.text,
                    self.state,
                    [
                        [
                            t.run,
                            t.key,
                            t.title,
                            t.goal,
                            list(t.acceptance),
                            list(t.depends_on),
                            t.wave,
                            t.hitl,
                        ]
                        for t in self.tickets
                    ],
                ]
            )
        )[:32]


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
    return canonical(
        {
            "kind": update.kind,
            "run": update.run,
            "link": update.link,
            "text": update.text,
            "state": update.state,
            "tickets": [
                {
                    "run": t.run,
                    "key": t.key,
                    "title": t.title,
                    "goal": t.goal,
                    "acceptance": list(t.acceptance),
                    "depends_on": list(t.depends_on),
                    "wave": t.wave,
                    "hitl": t.hitl,
                }
                for t in update.tickets
            ],
        }
    )


def update_load(document: str) -> TrackerUpdate:
    d = object_json(document)
    kind = text(d.get("kind"), "kind")
    if kind not in ("specification", "tickets", "state"):
        raise ValueError(f"Unknown tracker update: {kind}")
    return TrackerUpdate(
        cast(UpdateKind, kind),
        text(d.get("run"), "run"),
        text(d.get("link"), "link"),
        text(d.get("text", ""), "text"),
        text(d.get("state", ""), "state"),
        tuple(
            TicketMirror(
                text(t.get("run"), "run"),
                text(t.get("key"), "key"),
                text(t.get("title"), "title"),
                text(t.get("goal", ""), "goal"),
                tuple(text(x, "acceptance") for x in sequence(t.get("acceptance", []))),
                tuple(text(x, "dependency") for x in sequence(t.get("depends_on", []))),
                integer(t.get("wave", 1), "wave"),
                t.get("hitl") is True,
            )
            for t in (mapping(x) for x in sequence(d.get("tickets", [])))
        ),
    )


def receipt_json(receipt: TrackerReceipt) -> str:
    return canonical({"links": [list(pair) for pair in receipt.links]})
