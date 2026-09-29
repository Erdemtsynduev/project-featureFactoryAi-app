"""How each agent CLI is invoked and how its session output reads.

A dialect owns one provider's flags and output format; `CliHandler` owns
everything they share (prompt, schema, failures, the neutral result). Adding a
provider adds a dialect and never touches the others.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sdd_core.codec import integer, object_json
from sdd_core.models import Json, Usage
from sdd_core.sdk import STDOUT_LOG, Packet

from sdd_providers.budget import claude_settings

# The attempt files a dialect writes its result into and reads it back from.
SCHEMA_FILE = "schema.json"
OUTPUT_FILE = "agent-result.json"
STDOUT_FILE = STDOUT_LOG


@dataclass(frozen=True)
class Invocation:
    """One agent session to start: pinned executable, optional model, the step's packet."""

    executable: str
    model: str | None
    packet: Packet
    schema: dict[str, Json]

    @property
    def folder(self) -> Path:
        return Path(self.packet.directory)


@dataclass(frozen=True)
class Reply:
    """What one session returned, in provider-neutral terms."""

    document: dict[str, Json]
    usage: Usage = Usage()
    session: object = None
    model: object = None
    reported_cost: object = None


class ReportedError(Exception):
    """The provider finished but reported that the session failed."""


class Dialect(Protocol):
    def argv(self, call: Invocation) -> list[str]:
        """The command line; the prompt always travels on stdin."""
        ...

    def reply(self, folder: Path) -> Reply:
        """The structured result, usage and session of a successful exit."""
        ...


def _model(argv: list[str], model: str | None) -> list[str]:
    return argv + ["--model", model] if model else argv


def _count(raw: dict[str, Json], key: str, default: int | None = None) -> int:
    value = raw.get(key) if default is None else raw.get(key, default)
    return integer(value, key)


def uses_web(call: Invocation) -> bool:
    """The step grants its agent the internet (`tools: ["web"]` in its options)."""
    return "web" in call.packet.step.options.tools


class Claude:
    """Claude Code in print mode: one JSON envelope with the structured output."""

    READ_ONLY_TOOLS = ",Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell"
    COMMAND_TOOLS = "Bash,PowerShell"
    WEB_TOOLS = "WebSearch,WebFetch"

    def argv(self, call: Invocation) -> list[str]:
        mutates = call.packet.step.mutates
        argv = [
            call.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(call.schema),
            "--permission-mode",
            "acceptEdits",
            "--disallowedTools",
            "Agent,Task" + ("" if mutates else self.READ_ONLY_TOOLS),
        ]
        # acceptEdits approves edits only; a non-interactive session refuses every other
        # tool, so a working step could never run its checks nor a web step search.
        # Codex gets the same through its sandbox with approvals off.
        grants = ((self.COMMAND_TOOLS, mutates), (self.WEB_TOOLS, uses_web(call)))
        allowed = [tools for tools, granted in grants if granted]
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if mutates:
            # Commands near the deadline are refused by the engine's budget hook.
            argv += ["--settings", claude_settings(call.folder)]
        argv = _model(argv, call.model)
        if call.packet.resume:
            argv += ["--resume", call.packet.resume]
        return argv

    def reply(self, folder: Path) -> Reply:
        envelope = object_json((folder / STDOUT_FILE).read_text(encoding="utf-8"))
        if envelope.get("is_error") is True:
            raise ReportedError("reported an error")
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            raise ValueError("No structured result")
        return Reply(
            structured,
            self._usage(envelope.get("usage")),
            envelope.get("session_id"),
            self._billed_model(envelope.get("modelUsage")),
            envelope.get("total_cost_usd"),
        )

    @staticmethod
    def _billed_model(spent: Json) -> str | None:
        """The exact model id(s) Claude billed; the one that wrote most names the attempt."""
        if not isinstance(spent, dict) or not spent:
            return None

        def produced(name: str) -> int:
            entry = spent[name]
            value = entry.get("outputTokens") if isinstance(entry, dict) else None
            return value if isinstance(value, int) else 0

        return max(spent, key=produced)

    @staticmethod
    def _usage(raw: Json) -> Usage:
        """Input counts every prompt token, cached or not; the cache split is kept too."""
        if not isinstance(raw, dict):
            return Usage()
        read = _count(raw, "cache_read_input_tokens", 0)
        written = _count(raw, "cache_creation_input_tokens", 0)
        return Usage(
            _count(raw, "input_tokens") + read + written,
            _count(raw, "output_tokens"),
            read,
            written,
        )


class Codex:
    """Codex exec: JSON events on stdout, the structured result in its own file."""

    # Live web search as a config override, valid for `exec` and `exec resume` alike.
    WEB_CONFIG = ("-c", 'web_search="live"')

    def argv(self, call: Invocation) -> list[str]:
        packet = call.packet
        sandbox = "workspace-write" if packet.step.mutates else "read-only"
        output = [
            "--output-schema",
            str(call.folder / SCHEMA_FILE),
            "-o",
            str(call.folder / OUTPUT_FILE),
            *(self.WEB_CONFIG if uses_web(call) else ()),
        ]
        if packet.resume:
            # `exec resume` inherits the working directory and takes the sandbox as config.
            argv = [call.executable, "exec", "resume", "--json", "-c", 'approval_policy="never"']
            argv += ["-c", f'sandbox_mode="{sandbox}"', *output]
            return [*_model(argv, call.model), packet.resume, "-"]
        argv = [call.executable, "exec", "--json", "-C", packet.workspace, "--sandbox", sandbox]
        argv += ["-c", 'approval_policy="never"', *output]
        return [*_model(argv, call.model), "-"]

    def reply(self, folder: Path) -> Reply:
        document = object_json((folder / OUTPUT_FILE).read_text(encoding="utf-8"))
        usage, session = Usage(), None
        for line in (folder / STDOUT_FILE).read_text(encoding="utf-8").splitlines():
            event = object_json(line)
            if event.get("type") == "thread.started":
                session = event.get("thread_id")
            raw = event.get("usage")
            if event.get("type") == "turn.completed" and isinstance(raw, dict):
                usage = Usage(
                    _count(raw, "input_tokens"),
                    _count(raw, "output_tokens"),
                    _count(raw, "cached_input_tokens", 0),
                )
        return Reply(document, usage, session)


DIALECTS: dict[str, Dialect] = {"claude": Claude(), "codex": Codex()}
