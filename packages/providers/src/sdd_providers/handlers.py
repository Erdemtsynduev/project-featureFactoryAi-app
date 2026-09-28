"""Fresh CLI sessions and bounded command checks behind the same handler contract."""

import hashlib
import json
import re
import time
from pathlib import Path

from sdd_core.codec import canonical, flag, integer, object_json, text
from sdd_core.memory import notes, tickets_of
from sdd_core.models import Artifact, Json, Result, Usage
from sdd_core.options import StepOptions
from sdd_core.questions import questions
from sdd_core.sdk import Launch, Manifest, Packet

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
        settings = StepOptions.parse(packet.step.config)
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
        logs = [Path(packet.directory) / name for name in ("stdout.log", "stderr.log")]
        pattern = StepOptions.parse(packet.step.config).error_pattern
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
    "goal, testable acceptance criteria, ids of tickets it depends on and owned paths.\n"
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

TICKETS_SCHEMA: dict[str, Json] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "goal": {"type": "string"},
            "acceptance": STRINGS,
            "depends_on": STRINGS,
            "paths": STRINGS,
        },
        "required": ["id", "title", "goal", "acceptance", "depends_on", "paths"],
    },
}


def emits_tickets(packet: Packet) -> bool:
    """A planning step opts into ticket output through `{"produces": "tickets"}`."""
    return StepOptions.parse(packet.step.config).produces == "tickets"


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
    if emits_tickets(packet):
        properties["tickets"] = TICKETS_SCHEMA
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def instructions(packet: Packet) -> str:
    return PREAMBLE + (TICKETS_INSTRUCTION if emits_tickets(packet) else "")


def shared_data(doc: dict[str, Json], packet: Packet) -> dict[str, Json]:
    """Questions, notes and tickets an agent declared, validated before routing."""
    data: dict[str, Json] = {}
    for key in ("questions", "notes", "tickets"):
        value = doc.get(key)
        if value:
            data[key] = value
    if "tickets" in data and not emits_tickets(packet):
        del data["tickets"]
    facts = canonical(data)
    questions(facts)
    notes(facts)
    tickets_of(facts)
    return data


class CliHandler:
    def __init__(self, provider: str, executable: str, model: str | None = None) -> None:
        if provider not in ("claude", "codex"):
            raise ValueError("Unsupported provider")
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.lower() in (".cmd", ".bat", ".ps1")
        ):
            raise ValueError("Pin the real CLI executable, not a shell wrapper")
        self.provider, self.executable, self.model = provider, str(path), model
        self.manifest = Manifest(
            provider,
            "0.1.0",
            capabilities=("process", "agent", "resume"),
            settings=canonical(
                {
                    "executable": str(path),
                    "model": model,
                    "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ),
        )

    def prepare(self, packet: Packet) -> Launch:
        folder = Path(packet.directory)
        schema = result_schema(packet)
        schema_path = folder / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        if packet.resume:
            prompt = (
                "Continue the same bounded step in this session with the new input below, "
                "then return the same structured result.\n" + packet.context
            )
        else:
            prompt = instructions(packet) + packet.step.prompt + "\nContext:\n" + packet.context
        output = folder / "agent-result.json"
        sandbox = "workspace-write" if packet.step.mutates else "read-only"
        if self.provider == "codex" and packet.resume:
            # `exec resume` inherits the working directory and takes the sandbox as config.
            argv = [
                self.executable,
                "exec",
                "resume",
                "--json",
                "-c",
                'approval_policy="never"',
                "-c",
                f'sandbox_mode="{sandbox}"',
                "--output-schema",
                str(schema_path),
                "-o",
                str(output),
            ]
            if self.model:
                argv += ["--model", self.model]
            argv += [packet.resume, prompt]
        elif self.provider == "codex":
            argv = [
                self.executable,
                "exec",
                "--json",
                "-C",
                packet.workspace,
                "--sandbox",
                "workspace-write" if packet.step.mutates else "read-only",
                "-c",
                'approval_policy="never"',
                "--output-schema",
                str(schema_path),
                "-o",
                str(output),
            ]
            if self.model:
                argv += ["--model", self.model]
            argv += [prompt]
        else:
            argv = [
                self.executable,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
                "--permission-mode",
                "acceptEdits",
                "--disallowedTools",
                "Agent,Task"
                + (
                    ""
                    if packet.step.mutates
                    else ",Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell"
                ),
            ]
            if self.model:
                argv += ["--model", self.model]
            if packet.resume:
                argv += ["--resume", packet.resume]
        return Launch(tuple(argv), packet.workspace)

    def failure(self, packet: Packet, revision: str, summary: str) -> Result:
        """Recognised limits become a bounded wait; the engine resumes by itself.

        A rotation wrapper may shorten the wait and switch to another profile.
        """
        folder = Path(packet.directory)
        output = "\n".join(
            (folder / name).read_text(encoding="utf-8", errors="replace")
            for name in ("stdout.log", "stderr.log")
            if (folder / name).is_file()
        )
        now = time.time()
        found = classify(output, now)
        data = canonical(
            {"failure": found.kind, "handler": self.provider, "reset_at": found.reset_at}
        )
        waits = {"usage_limit": 3600.0, "rate_limit": 120.0, "unreachable": 60.0}
        if found.kind in waits:
            resume = min(found.reset_at or now + waits[found.kind], now + 6.5 * 86400)
            reason = f"{summary}: {found.kind.replace('_', ' ')} · {found.excerpt}"
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "waiting",
                reason,
                revision,
                resume_at=resume,
                data=data,
            )
        hint = " · run the CLI login" if found.kind == "authentication" else ""
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            "blocked",
            f"{summary}; {found.kind.replace('_', ' ')}{hint}: {found.excerpt or 'inspect stderr.log'}",
            revision,
            data=data,
        )

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        folder = Path(packet.directory)
        if exit_code != 0:
            return self.failure(packet, revision, f"{self.provider} exit {exit_code}")
        usage = Usage()
        session: object = None
        model: object = self.model
        reported: object = None
        if self.provider == "codex":
            doc = object_json((folder / "agent-result.json").read_text(encoding="utf-8"))
            for line in (folder / "stdout.log").read_text(encoding="utf-8").splitlines():
                item = object_json(line)
                if item.get("type") == "thread.started":
                    session = item.get("thread_id")
                raw_usage = item.get("usage")
                if item.get("type") == "turn.completed" and isinstance(raw_usage, dict):
                    usage = Usage(
                        integer(raw_usage.get("input_tokens"), "input_tokens"),
                        integer(raw_usage.get("output_tokens"), "output_tokens"),
                        integer(raw_usage.get("cached_input_tokens", 0), "cached_input_tokens"),
                    )
        else:
            envelope = object_json((folder / "stdout.log").read_text(encoding="utf-8"))
            if envelope.get("is_error") is True:
                return self.failure(packet, revision, f"{self.provider} reported an error")
            structured = envelope.get("structured_output")
            if not isinstance(structured, dict):
                raise ValueError("No structured result")
            doc = structured
            session = envelope.get("session_id")
            # The exact model id(s) Claude billed; the largest one names the attempt.
            spent = envelope.get("modelUsage")
            if isinstance(spent, dict) and spent:

                def produced(name: str) -> int:
                    entry = spent[name]
                    value = entry.get("outputTokens") if isinstance(entry, dict) else None
                    return value if isinstance(value, int) else 0

                model = max(spent, key=produced)
            reported = envelope.get("total_cost_usd")
            raw_usage = envelope.get("usage")
            if isinstance(raw_usage, dict):
                usage = Usage(
                    integer(raw_usage.get("input_tokens"), "input_tokens")
                    + integer(
                        raw_usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"
                    )
                    + integer(
                        raw_usage.get("cache_creation_input_tokens", 0),
                        "cache_creation_input_tokens",
                    ),
                    integer(raw_usage.get("output_tokens"), "output_tokens"),
                    integer(raw_usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"),
                    integer(
                        raw_usage.get("cache_creation_input_tokens", 0),
                        "cache_creation_input_tokens",
                    ),
                )
        data: dict[str, Json] = {}
        if isinstance(model, str) and model:
            data["model"] = model
        if isinstance(reported, (int, float)):
            data["reported_cost_usd"] = float(reported)
        data.update(shared_data(doc, packet))  # malformed output fails before routing
        if isinstance(session, str) and session:
            data["session"] = {"id": session, "step": packet.step.id, "handler": self.provider}
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            text(doc.get("outcome"), "outcome"),
            text(doc.get("reason"), "reason"),
            revision,
            (artifact(folder / "stdout.log", packet, revision),),
            usage,
            flag(doc.get("standards"), "standards"),
            flag(doc.get("specification"), "specification"),
            data=canonical(data),
        )
