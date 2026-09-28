"""Immutable wire contracts. All timestamps and identifiers are supplied by callers."""

from dataclasses import dataclass
from typing import Literal

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Kind = Literal["agent", "check", "human", "condition", "operation", "finish"]
type Status = Literal["ready", "running", "waiting", "blocked", "accepted"]


@dataclass(frozen=True)
class Step:
    id: str
    kind: Kind
    handler: str = ""
    prompt: str = ""
    transitions: tuple[tuple[str, str], ...] = ()
    required: bool = False
    gate: bool = False
    mutates: bool = False
    max_visits: int = 3
    timeout: int = 900
    profile: str = "default"
    config: str = "{}"
    condition_key: str = ""
    condition_value: str = ""


@dataclass(frozen=True)
class Workflow:
    id: str
    entry: str
    steps: tuple[Step, ...]
    schema: int = 1
    max_calls: int = 10
    max_input_chars: int = 24000
    max_tokens: int | None = None
    max_planning_calls: int | None = None

    def step(self, identifier: str) -> Step:
        return next(step for step in self.steps if step.id == identifier)


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str
    revision: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None


@dataclass(frozen=True)
class Result:
    attempt_id: str
    generation: int
    outcome: str
    reason: str
    revision: str
    artifacts: tuple[Artifact, ...] = ()
    usage: Usage = Usage()
    standards: bool | None = None
    specification: bool | None = None
    resume_at: float | None = None
    data: str = "{}"


@dataclass(frozen=True)
class Attempt:
    id: str
    step: str
    generation: int
    started: float
    deadline: float
    base_revision: str
    previous: str | None = None


@dataclass(frozen=True)
class Run:
    id: str
    workflow_digest: str
    step: str
    revision: str
    status: Status = "ready"
    paused: bool = True
    version: int = 0
    generation: int = 0
    active: Attempt | None = None
    visits: tuple[tuple[str, int], ...] = ()
    completed: tuple[str, ...] = ()
    gates: tuple[tuple[str, str], ...] = ()
    calls: int = 0
    tokens: int = 0
    usage_unknown: bool = False
    infrastructure_failures: int = 0
    wake_at: float | None = None
    reason: str = ""
    previous_attempt: str | None = None
    planning_calls: int = 0


@dataclass(frozen=True)
class Event:
    kind: str
    at: float
    detail: str


@dataclass(frozen=True)
class Effect:
    id: str
    kind: str
    attempt: Attempt


@dataclass(frozen=True)
class Transition:
    state: Run
    events: tuple[Event, ...]
    effects: tuple[Effect, ...] = ()
