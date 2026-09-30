"""Fresh CLI sessions and bounded command checks behind the same handler contract."""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sdd_core.codec import canonical, flag, text
from sdd_core.memory import notes
from sdd_core.models import PLAN_CHANGE_KINDS, TICKET_NEEDS, Artifact, Json, Result
from sdd_core.plan_changes import plan_changes_of
from sdd_core.questions import questions
from sdd_core.sdk import STDERR_LOG, STDOUT_LOG, Launch, Manifest, Packet
from sdd_core.tickets import tickets_of

from sdd_providers import budget
from sdd_providers.dialects import DIALECTS, SCHEMA_FILE, STDOUT_FILE, Invocation, ReportedError
from sdd_providers.failures import classify


def artifact(path: Path, packet: Packet, revision: str) -> Artifact:
    return Artifact(
        path.resolve().relative_to(Path(packet.workspace).resolve()).as_posix(),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        revision,
    )


class CommandHandler:
    manifest = Manifest("command", "0.1.0", capabilities=("process", "check", "operation"))

    def prepare(self, packet: Packet) -> Launch:
        settings = packet.step.options
        argv = settings.argv
        if (
            not argv
            or not Path(argv[0]).is_absolute()
            or Path(argv[0]).suffix.lower() in (".cmd", ".bat", ".ps1")
        ):
            raise ValueError(
                "An explicit executable path is required; shell wrappers are not accepted"
            )
        root = Path(packet.workspace).resolve()
        cwd = (root / settings.cwd).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            raise ValueError("Check cwd must be an existing folder inside the workspace")
        return Launch(argv, str(cwd))

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        logs = [Path(packet.directory) / name for name in (STDOUT_LOG, STDERR_LOG)]
        pattern = packet.step.options.error_pattern
        failed = exit_code != 0 or bool(
            pattern
            and any(
                re.search(pattern, path.read_text(encoding="utf-8", errors="replace"))
                for path in logs
            )
        )
        outcome = "failed" if failed else ("passed" if packet.step.gate else "done")
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            outcome,
            f"Command exit {exit_code}",
            revision,
            tuple(artifact(path, packet, revision) for path in logs),
        )


PREAMBLE = (
    "Execute only this bounded step. Read applicable AGENTS.md. Do not start agents, "
    "change engine state, install tools or publish; commit only when the step "
    "instructions authorize it. Only the engine accepts work.\n"
    "If a decision only a human can make blocks you and the step declares a `questions` "
    "outcome, return it and list each decision in `questions`: a short id, the question, "
    "2-4 concrete options and the one option you recommend. Otherwise return [].\n"
    "`notes` is the task memory shared with later steps and other agents: 0-5 short "
    "durable facts they must know (decisions, owned paths, constraints, gotchas). "
    "Do not repeat notes already listed under Task memory; return [] when nothing is new.\n"
)

TICKETS_INSTRUCTION = (
    "Return the breakdown in `tickets`: each ticket has a short unique id, a title, the "
    "goal, testable acceptance criteria, ids of tickets it depends on, owned paths and "
    "`needs`: `human` when a person must decide or review, `asset` when it needs files "
    "agents cannot make or obtain (licensed recordings, purchased models), `web` when its "
    "agent needs the internet; [] otherwise. `depends_on` names tickets of this breakdown "
    "only; `after` names run ids of existing tickets the brief lists that it waits for, [] "
    "otherwise.\n"
)
PLAN_CHANGES_INSTRUCTION = (
    "Return proposed corrections in `plan_changes`, [] when the plan is sound. Each has "
    "`kind` and `reason` (why, for the person approving). `revise`: one draft replacing a "
    "never-started ticket (same id). `merge`: `tickets` two or more never-started ids, "
    "one draft keeping one of those ids. `split`: `ticket` never started, two or more "
    "drafts with new ids. `cancel`: `ticket` never started. `need`: `ticket` and its "
    "`needs`. `guide`: `ticket`, `text` for its next attempt, `retry` to run it again. "
    "`reflow`: `ticket` not running and its `flow` changes: `skip` a `step` of its flow "
    "(`required` true to skip one marked *, the person approving decides), or `profile` "
    "to run a `step` with another agent `profile`. Unused fields are empty.\n"
)

STRINGS: dict[str, Json] = {"type": "array", "items": {"type": "string"}}

QUESTIONS_SCHEMA: dict[str, Json] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "question": {"type": "string"},
            "options": STRINGS,
            "recommended": {"type": "string"},
        },
        "required": ["id", "question", "options", "recommended"],
    },
}

NEEDS: dict[str, Json] = {"type": "array", "items": {"enum": list[Json](TICKET_NEEDS)}}


def _strict(properties: dict[str, Json]) -> dict[str, Json]:
    """An object schema where every property is required, as strict structured output asks."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


TICKET: dict[str, Json] = _strict(
    {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "acceptance": STRINGS,
        "depends_on": STRINGS,
        "paths": STRINGS,
        "needs": NEEDS,
        "after": STRINGS,
    }
)
TICKETS_SCHEMA: dict[str, Json] = {"type": "array", "items": TICKET}
PLAN_CHANGES_SCHEMA: dict[str, Json] = {
    "type": "array",
    "items": _strict(
        {
            "kind": {"enum": list[Json](PLAN_CHANGE_KINDS)},
            "reason": {"type": "string"},
            "ticket": {"type": "string"},
            "tickets": STRINGS,
            "drafts": TICKETS_SCHEMA,
            "needs": NEEDS,
            "text": {"type": "string"},
            "retry": {"type": "boolean"},
            "flow": {
                "type": "array",
                "items": _strict(
                    {
                        "kind": {"enum": ["skip", "profile"]},
                        "step": {"type": "string"},
                        "profile": {"type": "string"},
                        "required": {"type": "boolean"},
                    }
                ),
            },
        }
    ),
}


@dataclass(frozen=True)
class ProductOutput:
    """The extra result field a product step returns, and how it is checked."""

    key: str
    schema: dict[str, Json]
    instruction: str
    check: Callable[[str], object]


# One row per product whose result carries structured output beyond the common fields.
PRODUCT_OUTPUTS: dict[str, ProductOutput] = {
    "tickets": ProductOutput("tickets", TICKETS_SCHEMA, TICKETS_INSTRUCTION, tickets_of),
    "plan_changes": ProductOutput(
        "plan_changes", PLAN_CHANGES_SCHEMA, PLAN_CHANGES_INSTRUCTION, plan_changes_of
    ),
}


def product_output(packet: Packet) -> ProductOutput | None:
    """The structured output the step declares through `{"produces": ...}`, if any."""
    return PRODUCT_OUTPUTS.get(packet.step.options.produces)


def result_schema(packet: Packet) -> dict[str, Json]:
    """The structured result contract; ticket output only where a step declares it."""
    properties: dict[str, Json] = {
        "outcome": {"type": "string", "enum": [*dict(packet.step.transitions), "blocked"]},
        "reason": {"type": "string"},
        "standards": {"type": "boolean"},
        "specification": {"type": "boolean"},
        "questions": QUESTIONS_SCHEMA,
        "notes": STRINGS,
    }
    output = product_output(packet)
    if output is not None:
        properties[output.key] = output.schema
    return _strict(properties)


def instructions(packet: Packet) -> str:
    output = product_output(packet)
    return PREAMBLE + (output.instruction if output is not None else "")


def time_budget(packet: Packet) -> str:
    """The attempt's wall-clock bound, stated so the agent can plan within it.

    A stopped attempt keeps its files but not its result: work in flight at the
    limit only reaches a read-only reconciliation.
    """
    minutes = max(1, round((packet.attempt.deadline - packet.attempt.started) / 60))
    return (
        f"Time budget: the engine stops this step {minutes} min after it starts. "
        "A stopped step returns no result and its unfinished work is only reconciled. "
        "Do not start a command that may not finish well inside the budget; leave long "
        "verification to the workflow's check steps. Before the last tenth of the budget, "
        "leave the workspace coherent and return your result.\n"
    )


def shared_data(doc: dict[str, Json], packet: Packet) -> dict[str, Json]:
    """Questions, notes and the step's product an agent declared, validated before routing."""
    output = product_output(packet)
    keys = ("questions", "notes", *((output.key,) if output is not None else ()))
    data: dict[str, Json] = {key: doc[key] for key in keys if doc.get(key)}
    facts = canonical(data)
    questions(facts)
    notes(facts)
    if output is not None:
        output.check(facts)
    return data


# Recognised limits wait this long when the provider names no reset time.
WAITS = {"usage_limit": 3600.0, "rate_limit": 120.0, "unreachable": 60.0}
# Stay inside the engine's one-week bound on any wait.
MAX_WAIT = 6.5 * 86400


class CliHandler:
    """A fresh (or resumed) agent CLI session; its dialect owns the provider's flags."""

    def __init__(self, provider: str, executable: str, model: str | None = None) -> None:
        dialect = DIALECTS.get(provider)
        if dialect is None:
            raise ValueError("Unsupported provider")
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.lower() in (".cmd", ".bat", ".ps1")
        ):
            raise ValueError("Pin the real CLI executable, not a shell wrapper")
        self.provider, self.executable, self.model = provider, str(path), model
        self.dialect = dialect
        self.manifest = Manifest(
            provider,
            "0.1.0",
            capabilities=("process", "agent", "resume", "web"),
            settings=canonical(
                {
                    "executable": str(path),
                    "model": model,
                    "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ),
        )

    def prepare(self, packet: Packet) -> Launch:
        schema = result_schema(packet)
        (Path(packet.directory) / SCHEMA_FILE).write_text(json.dumps(schema), encoding="utf-8")
        budget.write(packet)
        if packet.resume:
            prompt = (
                "Continue the same bounded step in this session with the new input below, "
                "then return the same structured result.\n" + time_budget(packet) + packet.context
            )
        else:
            prompt = (
                instructions(packet)
                + time_budget(packet)
                + packet.step.prompt
                + "\nContext:\n"
                + packet.context
            )
        argv = self.dialect.argv(Invocation(self.executable, self.model, packet, schema))
        # The dialect reads the prompt from stdin: a long brief never reaches the command line.
        return Launch(tuple(argv), packet.workspace, input=prompt)

    def failure(self, packet: Packet, revision: str, summary: str) -> Result:
        """Recognised limits become a bounded wait; the engine resumes by itself.

        A rotation wrapper may shorten the wait and switch to another profile.
        """
        folder = Path(packet.directory)
        output = "\n".join(
            (folder / name).read_text(encoding="utf-8", errors="replace")
            for name in (STDOUT_LOG, STDERR_LOG)
            if (folder / name).is_file()
        )
        now = time.time()
        found = classify(output, now)
        data = canonical(
            {"failure": found.kind, "handler": self.provider, "reset_at": found.reset_at}
        )
        kind = found.kind.replace("_", " ")
        if found.kind in WAITS:
            resume = min(found.reset_at or now + WAITS[found.kind], now + MAX_WAIT)
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "waiting",
                f"{summary}: {kind} · {found.excerpt}",
                revision,
                resume_at=resume,
                data=data,
            )
        hint = " · run the CLI login" if found.kind == "authentication" else ""
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            "blocked",
            f"{summary}; {kind}{hint}: {found.excerpt or 'inspect stderr.log'}",
            revision,
            data=data,
        )

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        folder = Path(packet.directory)
        if exit_code != 0:
            return self.failure(packet, revision, f"{self.provider} exit {exit_code}")
        try:
            reply = self.dialect.reply(folder)
        except ReportedError as error:
            return self.failure(packet, revision, f"{self.provider} {error}")
        doc = reply.document
        data: dict[str, Json] = {}
        model = reply.model if reply.model is not None else self.model
        if isinstance(model, str) and model:
            data["model"] = model
        if isinstance(reply.reported_cost, (int, float)):
            data["reported_cost_usd"] = float(reply.reported_cost)
        data.update(shared_data(doc, packet))  # malformed output fails before routing
        if isinstance(reply.session, str) and reply.session:
            data["session"] = {
                "id": reply.session,
                "step": packet.step.id,
                "handler": self.provider,
            }
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            text(doc.get("outcome"), "outcome"),
            text(doc.get("reason"), "reason"),
            revision,
            (artifact(folder / STDOUT_FILE, packet, revision),),
            reply.usage,
            flag(doc.get("standards"), "standards"),
            flag(doc.get("specification"), "specification"),
            data=canonical(data),
        )
