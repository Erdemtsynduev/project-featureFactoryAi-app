"""Immutable wire contracts. All timestamps and identifiers are supplied by callers.

The closed vocabularies (step kinds, run statuses, event kinds, operator commands)
are declared once here; validation, the JSON schema and the codec read them from
these types instead of repeating the spelling.
"""

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, get_args

from sdd_core.wire import Json

if TYPE_CHECKING:
    from sdd_core.options import StepOptions

__all__ = ["Json"]

type Kind = Literal["agent", "check", "human", "condition", "operation", "finish"]
type Status = Literal["ready", "running", "waiting", "blocked", "accepted"]
# Why a run is held: blocked by a rule, or stopped by its operator. "" means not held.
# Behaviour reads the cause; `Run.reason` is only the text people see.
type Cause = Literal[
    "",
    "stop",
    "blocked",
    "call_limit",
    "planning_limit",
    "token_limit",
    "visit_limit",
    "queue_limit",
    "wait_limit",
    "recovery_limit",
    "uncertain",
    "acceptance",
    "workspace_changed",
]
# Tools a step may grant its agent beyond editing and commands.
type Tool = Literal["web"]
# What a ticket needs besides an agent run: a person's decision or review, a file the
# agents cannot make or obtain (a licensed recording, a model), or the internet.
type TicketNeed = Literal["human", "asset", "web"]
# Corrections a plan review may propose for an approved ticket breakdown.
type PlanChangeKind = Literal["revise", "merge", "split", "cancel", "need", "guide"]
type Command = Literal["stop", "pause", "resume", "auto", "manual", "retry"]
type EventKind = Literal[
    "accepted",
    "acceptance_rejected",
    "auto_answer_disabled",
    "auto_answer_enabled",
    "blocked",
    "calls_granted",
    "condition_evaluated",
    "condition_released",
    "dispatched",
    "lane_opened",
    "revision_rebased",
    "limit",
    "operator_message",
    "paused",
    "plan_revised",
    "reconciliation_requested",
    "recovery_exhausted",
    "recovery_scheduled",
    "restarted",
    "result_applied",
    "resumed",
    "retry_requested",
    "revision_changed",
    "stop_requested",
    "uncertain",
    "waiting",
    "waiting_exhausted",
]

KINDS: tuple[Kind, ...] = get_args(Kind.__value__)
STATUSES: tuple[Status, ...] = get_args(Status.__value__)
CAUSES: tuple[Cause, ...] = get_args(Cause.__value__)
# The statuses each status may change into. A transition outside this table is a bug
# in the state machine; `accepted` is final.
STATUS_CHANGES: dict[Status, frozenset[Status]] = {
    "ready": frozenset({"ready", "running", "blocked", "accepted"}),
    "running": frozenset({"running", "ready", "waiting", "blocked"}),
    "waiting": frozenset({"waiting", "running", "ready", "blocked", "accepted"}),
    "blocked": frozenset({"blocked", "ready", "waiting"}),
    "accepted": frozenset({"accepted"}),
}
COMMANDS: tuple[Command, ...] = get_args(Command.__value__)
TOOLS: tuple[Tool, ...] = get_args(Tool.__value__)
TICKET_NEEDS: tuple[TicketNeed, ...] = get_args(TicketNeed.__value__)
PLAN_CHANGE_KINDS: tuple[PlanChangeKind, ...] = get_args(PlanChangeKind.__value__)
# Needs no agent step can meet: such a ticket waits for a person.
HELD_NEEDS: tuple[TicketNeed, ...] = ("human", "asset")
# Kinds that launch a handler process and so take an execution slot.
PROCESS_KINDS: tuple[Kind, ...] = ("agent", "check", "operation")
# Effect kinds that launch no handler process, so they never lock a pinned manifest.
UNPINNED_KINDS: tuple[Kind, ...] = ("human", "condition")
# Kinds whose step may be a gate: they produce `passed` evidence without changing files.
GATE_KINDS: tuple[Kind, ...] = ("agent", "check")
# Statuses that already say where a run stands: final, or held with its own reason.
SETTLED_STATUSES: tuple[Status, ...] = ("blocked", "accepted")
# Effect statuses of an attempt whose process may still run.
LIVE_EFFECT_STATUSES = ("pending", "running", "uncertain")
# Provider failures that refuse an attempt before any model work: the attempt's
# reserved calls are returned when its result also measured no tokens.
REFUSALS = ("usage_limit", "rate_limit", "authentication", "unreachable", "model_not_available")
# Outcomes the engine interprets itself; a workflow cannot route them.
RESERVED_OUTCOMES = ("waiting", "blocked")
# Spelling of step ids (graph documents) and of run and attempt ids (commands).
STEP_ID = r"[a-z][a-z0-9_-]{0,63}"
RECORD_ID = r"[A-Za-z0-9_-]{1,96}"


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

    @property
    def options(self) -> "StepOptions":
        """The typed view of `config`; parsed once per distinct spelling."""
        from sdd_core.options import StepOptions

        return StepOptions.parse(self.config)

    def target(self, outcome: str) -> str | None:
        """The step an outcome routes to, or None when the step does not declare it."""
        return dict(self.transitions).get(outcome)


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
        for step in self.steps:
            if step.id == identifier:
                return step
        raise ValueError(f"Unknown step: {identifier}")


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
    # Model calls reserved for this attempt at dispatch, returned if none happened.
    calls: int = 0
    planning_calls: int = 0


@dataclass(frozen=True)
class Spend:
    """Model use of a run: calls reserved at dispatch, tokens measured in results."""

    calls: int = 0
    planning_calls: int = 0
    # Model calls the operator granted beyond the workflow's `max_calls`.
    granted_calls: int = 0
    tokens: int = 0
    # A result did not report its usage, so token budgets cannot be trusted.
    usage_unknown: bool = False

    def reserve(self, agent: bool, planning: bool) -> "Spend":
        """Count the call an attempt may make, before it runs."""
        return replace(
            self,
            calls=self.calls + int(agent),
            planning_calls=self.planning_calls + int(planning),
        )

    def measure(self, usage: "Usage", agent: bool) -> "Spend":
        """Add the tokens a result reported; an agent that reported none is unknown."""
        unreported = usage.input_tokens is None or usage.output_tokens is None
        return replace(
            self,
            tokens=self.tokens + (usage.input_tokens or 0) + (usage.output_tokens or 0),
            usage_unknown=self.usage_unknown or (agent and unreported),
        )

    def grant(self, calls: int) -> "Spend":
        return replace(self, granted_calls=self.granted_calls + calls)

    def release(self, attempt: "Attempt") -> "Spend":
        """Return what an attempt reserved: it provably reached no model."""
        return replace(
            self,
            calls=max(0, self.calls - attempt.calls),
            planning_calls=max(0, self.planning_calls - attempt.planning_calls),
        )


# Fields of a nested value that the wire spells at its owner's level.
FLAT = {"wire": "flat"}


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
    spend: Spend = field(default=Spend(), metadata=FLAT)
    infrastructure_failures: int = 0
    wake_at: float | None = None
    reason: str = ""
    cause: Cause = ""
    previous_attempt: str | None = None
    # Operator's choice: answer structured agent questions with their recommendations.
    auto_answer: bool = False


@dataclass(frozen=True)
class Event:
    kind: EventKind
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
