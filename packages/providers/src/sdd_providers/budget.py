"""The attempt's time budget, enforced inside the agent before each command it starts.

The engine stops an attempt at its deadline and a stopped attempt returns no
result, so a command started near the end costs the whole step. A provider hook
runs `python -m sdd_providers.budget <attempt folder>` before a shell command:
in the last tenth of the budget every command is refused, in the last quarter a
background one is, each time telling the agent to return its result. The rule
reads only the attempt's `budget.json` and the clock; any error allows the call,
so the hook can never break a session.
"""

import json
import sys
import time
from pathlib import Path

from sdd_core.codec import canonical
from sdd_core.sdk import Packet

BUDGET_FILE = "budget.json"
# Shares of the budget still left when commands, then background commands, are refused.
LAST_COMMANDS = 0.10
LAST_BACKGROUND = 0.25


def write(packet: Packet) -> Path:
    """Record the attempt's budget where its hook will read it."""
    path = Path(packet.directory) / BUDGET_FILE
    attempt = packet.attempt
    path.write_text(
        canonical({"started": attempt.started, "deadline": attempt.deadline}), encoding="utf-8"
    )
    return path


def interpreter() -> Path:
    """A console interpreter with these packages: a GUI `pythonw` has no stderr to speak."""
    current = Path(sys.executable)
    console = current.with_name("python.exe")
    return console if current.name.lower() == "pythonw.exe" and console.is_file() else current


def claude_settings(folder: Path) -> str:
    """Claude Code settings adding this attempt's budget hook before shell commands."""
    command = f'"{interpreter().as_posix()}" -m sdd_providers.budget "{folder.as_posix()}"'
    hook = {"type": "command", "command": command}
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [hook]}]}})


def refusal(started: float, deadline: float, now: float, background: bool) -> str | None:
    """Why a command may not start now, or None when it may."""
    total = deadline - started
    left = deadline - now
    if total <= 0:
        return None
    minutes = max(0, round(left / 60))
    if left <= total * LAST_COMMANDS:
        return (
            f"Time budget: about {minutes} min left before the engine stops this step, "
            "and a stopped step returns no result. Start no more commands: leave the "
            "workspace coherent and return your structured result now."
        )
    if background and left <= total * LAST_BACKGROUND:
        return (
            f"Time budget: about {minutes} min left. Start no background or long "
            "commands now; finish what is needed in the foreground and return your "
            "result before the budget ends."
        )
    return None


def hook(folder: Path, event: str, now: float) -> str | None:
    budget = json.loads((folder / BUDGET_FILE).read_text(encoding="utf-8"))
    call = json.loads(event) if event.strip() else {}
    arguments = call.get("tool_input") if isinstance(call, dict) else None
    background = isinstance(arguments, dict) and arguments.get("run_in_background") is True
    return refusal(float(budget["started"]), float(budget["deadline"]), now, background)


def main(argv: list[str]) -> int:
    try:
        reason = hook(Path(argv[1]), sys.stdin.read(), time.time())
    except Exception:
        return 0  # never break the session over its budget
    if reason is None:
        return 0
    sys.stderr.write(reason + "\n")
    return 2  # a PreToolUse hook's exit code 2 refuses the call and shows the reason


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
