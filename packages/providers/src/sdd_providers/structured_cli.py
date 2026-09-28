"""Fresh-session Cursor and OpenCode adapters with fail-closed result parsing.

These adapters have synthetic protocol coverage. Installed CLI versions must be
qualified with a bounded live smoke before being enabled for production work.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sdd_core.codec import canonical, flag, object_json, text
from sdd_core.models import Result
from sdd_core.sdk import Launch, Manifest, Packet

from sdd_providers.handlers import artifact
from sdd_providers.protocols import cursor_response, opencode_response, structured_answer


@dataclass(frozen=True)
class Invocation:
    executable: str
    arguments: tuple[str, ...] = ()

    def pinned(self) -> str:
        executable = Path(self.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("Pin an existing absolute executable")
        if executable.suffix.lower() in (".ps1", ".cmd", ".bat"):
            raise ValueError("Resolve wrappers to interpreter and explicit entry point")
        files = [executable]
        for argument in self.arguments:
            candidate = Path(argument)
            if candidate.is_absolute() and candidate.is_file():
                files.append(candidate)
        return canonical(
            {
                "executable": str(executable),
                "arguments": self.arguments,
                "files": {
                    str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
                },
            }
        )

    def argv(self, *arguments: str) -> tuple[str, ...]:
        return (self.executable, *self.arguments, *arguments)


class StructuredCliHandler:
    def __init__(self, provider: str, invocation: Invocation, model: str | None = None) -> None:
        if provider not in ("cursor", "opencode"):
            raise ValueError("Unsupported structured CLI")
        self.provider, self.invocation, self.model = provider, invocation, model
        self.manifest = Manifest(
            provider,
            "0.1.0",
            capabilities=("process", "agent"),
            settings=canonical({"invocation": invocation.pinned(), "model": model}),
        )

    def prepare(self, packet: Packet) -> Launch:
        # Refuse an invocation modified since profile binding.
        if object_json(self.manifest.settings)["invocation"] != self.invocation.pinned():
            raise ValueError("Pinned invocation changed")
        prompt = (
            "Execute only this bounded step. Follow AGENTS.md. Do not launch agents, commit, "
            "publish or modify engine state. Return only JSON with outcome, reason, standards "
            "and specification (the last two are booleans). Allowed outcomes: "
            + canonical(list(dict(packet.step.transitions)) + ["blocked"])
            + "\n"
            + packet.step.prompt
            + "\nContext:\n"
            + packet.context
        )
        if self.provider == "cursor":
            args = ["--print", "--output-format", "stream-json", "--workspace", packet.workspace]
            args += ["--force"] if packet.step.mutates else ["--mode", "ask"]
        else:
            if not packet.step.mutates:
                raise ValueError(
                    "OpenCode read-only policy is not qualified; choose another provider"
                )
            # Never connect to the user's shared background service.
            args = ["run", "--standalone", "--format", "json", "--auto"]
        if self.model:
            args += ["--model", self.model]
        return Launch(self.invocation.argv(*args, prompt), packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        if exit_code:
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                f"{self.provider} exited {exit_code}; inspect stderr.log",
                revision,
            )
        log = Path(packet.directory) / "stdout.log"
        events = [
            object_json(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        parser = {"cursor": cursor_response, "opencode": opencode_response}[self.provider]
        response, session = parser(events)
        doc = structured_answer(response)
        outcome = text(doc.get("outcome"), "outcome")
        if outcome not in dict(packet.step.transitions) and outcome != "blocked":
            raise ValueError("Undeclared provider outcome")
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            outcome,
            text(doc.get("reason"), "reason"),
            revision,
            (artifact(log, packet, revision),),
            standards=flag(doc.get("standards"), "standards"),
            specification=flag(doc.get("specification"), "specification"),
            data=canonical({"provider": self.provider, "session_id": session}),
        )
