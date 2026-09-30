"""Typed step options: what a step's `config` may say, and nothing else.

A step's configuration travels as canonical JSON (it is part of the published,
content-addressed workflow), but every reader goes through `StepOptions`, so a
misspelt or mistyped option is refused when the workflow is published instead of
being silently ignored while it runs.
"""

from dataclasses import dataclass, field, fields, replace
from functools import lru_cache

from sdd_core.models import TOOLS
from sdd_core.wire import Json, canonical, object_json

PURPOSES = ("", "planning")
TEXT_OPTIONS = (
    "purpose",
    "produces",
    "recovery_step",
    "auto_answer",
    "auto_outcome",
    "title",
    "cwd",
    "error_pattern",
    "commit_message",
    "pin_message",
)
# Options that are lists of text.
LIST_OPTIONS = ("argv", "tools")
PRODUCTS = ("", "specification", "tickets", "features", "plan_changes")
AUTO_ANSWERS = ("", "recommended")


def _text(value: Json, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Step option {name} must be text")
    return value


@dataclass(frozen=True)
class StepOptions:
    # Planning calls count against the separate planning budgets.
    purpose: str = ""
    # What the step's result is for the factory: a specification, a breakdown into
    # tickets or features, or changes to a breakdown.
    produces: str = ""
    # The read-only agent step that reconciles this step after an interruption.
    recovery_step: str = ""
    # A human step the agent may answer with its recommendations, and the outcome then.
    auto_answer: str = ""
    auto_outcome: str = ""
    # Questions a human step asks when no agent asked them.
    questions: tuple[Json, ...] = ()
    # A friendly step title for people.
    title: str = ""
    # Command steps: an explicit executable and arguments, the folder, a failure pattern.
    argv: tuple[str, ...] = ()
    # Commit message templates of lane steps: the project's commit convention.
    commit_message: str = ""
    pin_message: str = ""
    # Tools the agent may use beyond edits and commands (see `models.TOOLS`).
    tools: tuple[str, ...] = ()
    cwd: str = "."
    error_pattern: str = ""
    # The named agent profile resolved when the run was created.
    profile_snapshot: dict[str, Json] | None = None
    # Options this engine does not know; publication refuses them.
    unknown: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def parse(cls, config: str) -> "StepOptions":
        return _parse(config or "{}")

    @classmethod
    def _read(cls, config: str) -> "StepOptions":
        raw = object_json(config)
        known = {item.name for item in fields(cls)} - {"unknown"}
        values: dict[str, object] = {}
        for name in TEXT_OPTIONS:
            if name in raw:
                values[name] = _text(raw[name], name)
        for name in LIST_OPTIONS:
            if name in raw:
                items = raw[name]
                if not isinstance(items, list):
                    raise ValueError(f"Step option {name} must be a list")
                values[name] = tuple(_text(item, name) for item in items)
        if "questions" in raw:
            items = raw["questions"]
            if not isinstance(items, list):
                raise ValueError("Step option questions must be a list")
            values["questions"] = tuple(items)
        if "profile_snapshot" in raw:
            snapshot = raw["profile_snapshot"]
            if not isinstance(snapshot, dict):
                raise ValueError("Step option profile_snapshot must be an object")
            values["profile_snapshot"] = snapshot
        options = cls(**values, unknown=tuple(sorted(set(raw) - known)))  # type: ignore[arg-type]
        if options.purpose not in PURPOSES:
            raise ValueError(f"Unknown step purpose: {options.purpose}")
        if options.produces not in PRODUCTS:
            raise ValueError(f"Unknown step product: {options.produces}")
        if options.auto_answer not in AUTO_ANSWERS:
            raise ValueError(f"Unknown auto answer policy: {options.auto_answer}")
        if set(options.tools) - set(TOOLS):
            raise ValueError(
                f"Unknown step tools: {', '.join(sorted(set(options.tools) - set(TOOLS)))}"
            )
        return options

    @property
    def planning(self) -> bool:
        return self.purpose == "planning"

    def render(self) -> str:
        """Canonical JSON with only the options that differ from their defaults."""
        document: dict[str, Json] = {}
        for item in fields(self):
            if item.name == "unknown":
                continue
            value = getattr(self, item.name)
            if value == item.default:
                continue
            document[item.name] = list[Json](value) if isinstance(value, tuple) else value
        return canonical(document)

    def changed(self, **changes: object) -> "StepOptions":
        return replace(self, **changes)  # type: ignore[arg-type]

    def auto_answer_outcome(self, transitions: tuple[tuple[str, str], ...]) -> str:
        """The outcome an automatic answer records: the declared one, else the first route."""
        return self.auto_outcome or transitions[0][0]


@lru_cache(maxsize=4096)
def _parse(config: str) -> StepOptions:
    # Options are immutable and a config string is content: parse each spelling once.
    return StepOptions._read(config)
