"""Fresh CLI sessions and bounded command checks behind the same handler contract."""

import hashlib
import json
import re
from pathlib import Path

from sdd_core.codec import canonical, flag, integer, object_json, sequence, text
from sdd_core.models import Artifact, Result, Usage
from sdd_core.sdk import Launch, Manifest, Packet


def artifact(path: Path, packet: Packet, revision: str) -> Artifact:
    return Artifact(
        path.resolve().relative_to(Path(packet.workspace).resolve()).as_posix(),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        revision,
    )


class CommandHandler:
    manifest = Manifest("command", "0.1.0", capabilities=("process", "check", "operation"))

    def prepare(self, packet: Packet) -> Launch:
        config = object_json(packet.step.config)
        argv = tuple(text(x, "argv") for x in sequence(config.get("argv")))
        if (
            not argv
            or not Path(argv[0]).is_absolute()
            or Path(argv[0]).suffix.lower() in (".cmd", ".bat", ".ps1")
        ):
            raise ValueError(
                "An explicit executable path is required; shell wrappers are not accepted"
            )
        return Launch(argv, packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        config = object_json(packet.step.config)
        logs = [Path(packet.directory) / name for name in ("stdout.log", "stderr.log")]
        pattern = text(config.get("error_pattern", ""), "error_pattern")
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
            capabilities=("process", "agent"),
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
        outcomes = list(dict(packet.step.transitions)) + ["blocked"]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {"type": "string", "enum": outcomes},
                "reason": {"type": "string"},
                "standards": {"type": "boolean"},
                "specification": {"type": "boolean"},
            },
            "required": ["outcome", "reason", "standards", "specification"],
        }
        schema_path = folder / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        prompt = (
            "Execute only this bounded step. Read applicable AGENTS.md. Do not start agents, "
            "change engine state, install tools, commit or publish. Only the engine accepts work.\n"
            + packet.step.prompt
            + "\nContext:\n"
            + packet.context
        )
        output = folder / "agent-result.json"
        if self.provider == "codex":
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
        return Launch(tuple(argv), packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        folder = Path(packet.directory)
        if exit_code != 0:
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                f"{self.provider} exit {exit_code}; inspect stderr.log",
                revision,
            )
        usage = Usage()
        if self.provider == "codex":
            doc = object_json((folder / "agent-result.json").read_text(encoding="utf-8"))
            for line in (folder / "stdout.log").read_text(encoding="utf-8").splitlines():
                item = object_json(line)
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
                raise ValueError("Provider reported failure")
            structured = envelope.get("structured_output")
            if not isinstance(structured, dict):
                raise ValueError("No structured result")
            doc = structured
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
        )
