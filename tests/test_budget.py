import json
import subprocess
import sys
import time
from pathlib import Path

from sdd_core.models import Attempt, Step
from sdd_core.sdk import Packet
from sdd_providers import budget
from sdd_providers.handlers import CliHandler


def packet(tmp_path, mutates=True, started=0.0, deadline=1000.0):
    folder = tmp_path / "attempt"
    folder.mkdir(exist_ok=True)
    step = Step("work", "agent", "claude", "Work", (("done", "finish"),), mutates=mutates)
    return Packet(
        "r",
        Attempt("a1", "work", 1, started, deadline, "rev"),
        step,
        str(tmp_path),
        str(folder),
        "",
    )


def test_commands_are_refused_only_near_the_end_of_the_budget():
    assert budget.refusal(0, 1000, 500, background=True) is None
    background = budget.refusal(0, 1000, 800, background=True)
    assert background and "background" in background
    assert budget.refusal(0, 1000, 800, background=False) is None
    last = budget.refusal(0, 1000, 950, background=False)
    assert last and "return your structured result now" in last
    assert budget.refusal(0, 0, 10, background=False) is None


def hook(folder, event):
    return subprocess.run(
        [str(budget.interpreter()), "-m", "sdd_providers.budget", str(folder)],
        input=json.dumps(event).encode(),
        capture_output=True,
        timeout=30,
    )


def test_the_hook_refuses_with_exit_code_two_and_explains_why(tmp_path):
    now = time.time()
    folder = Path(budget.write(packet(tmp_path, started=now - 950, deadline=now + 50)).parent)
    refused = hook(folder, {"tool_name": "Bash", "tool_input": {"command": "sleep 600"}})
    assert refused.returncode == 2 and b"Time budget" in refused.stderr
    budget.write(packet(tmp_path, started=now, deadline=now + 1000))
    assert hook(folder, {"tool_name": "Bash", "tool_input": {}}).returncode == 0
    # A broken budget file never breaks the session.
    (folder / budget.BUDGET_FILE).write_text("{", encoding="utf-8")
    assert hook(folder, {"tool_name": "Bash"}).returncode == 0


def test_claude_working_steps_carry_the_budget_hook(tmp_path):
    claude = CliHandler("claude", sys.executable, "opus")
    working = claude.prepare(packet(tmp_path, mutates=True))
    settings = json.loads(working.argv[working.argv.index("--settings") + 1])
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash|PowerShell"
    assert "sdd_providers.budget" in entry["hooks"][0]["command"]
    assert (tmp_path / "attempt" / budget.BUDGET_FILE).is_file()
    reading = claude.prepare(packet(tmp_path, mutates=False))
    assert "--settings" not in reading.argv, "a read-only step runs no commands"
