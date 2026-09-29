"""Each agent CLI dialect: exact command lines, output parsing and failure policy."""

import json
import sys
import time
from pathlib import Path

import pytest
from sdd_core.models import Attempt, Step, Usage
from sdd_core.sdk import Packet
from sdd_providers.dialects import DIALECTS, Claude, Codex, Invocation, ReportedError
from sdd_providers.handlers import MAX_WAIT, WAITS, CliHandler

SCHEMA = {"type": "object"}


def packet(tmp_path: Path, *, mutates: bool = False, resume: str | None = None) -> Packet:
    folder = tmp_path / "attempt"
    folder.mkdir(exist_ok=True)
    step = Step("work", "agent", "x", "Do it", (("done", "finish"),), mutates=mutates)
    return Packet(
        "run", Attempt("a1", "work", 1, 0, 10, "rev"), step, str(tmp_path), str(folder), "C", resume
    )


def call(tmp_path: Path, model: str | None = None, **options: object) -> Invocation:
    return Invocation("/bin/agent", model, packet(tmp_path, **options), SCHEMA)  # type: ignore[arg-type]


def test_every_supported_provider_has_a_dialect():
    assert set(DIALECTS) == {"claude", "codex"}
    with pytest.raises(ValueError, match="Unsupported provider"):
        CliHandler("gemini", sys.executable)


def test_claude_read_only_steps_deny_edits_and_commands(tmp_path):
    argv = Claude().argv(call(tmp_path))
    assert argv == [
        "/bin/agent",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--permission-mode",
        "acceptEdits",
        "--disallowedTools",
        "Agent,Task,Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell",
    ]


def test_claude_working_steps_may_run_commands_and_resume(tmp_path):
    argv = Claude().argv(call(tmp_path, "opus", mutates=True, resume="s-1"))
    assert argv[argv.index("--disallowedTools") + 1] == "Agent,Task"
    assert argv[argv.index("--allowedTools") + 1] == "Bash,PowerShell"
    assert "sdd_providers.budget" in argv[argv.index("--settings") + 1]
    assert argv[-4:] == ["--model", "opus", "--resume", "s-1"]


def test_codex_fresh_session_names_workspace_sandbox_and_reads_stdin(tmp_path):
    invocation = call(tmp_path, "gpt", mutates=True)
    folder = invocation.folder
    assert Codex().argv(invocation) == [
        "/bin/agent",
        "exec",
        "--json",
        "-C",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "--output-schema",
        str(folder / "schema.json"),
        "-o",
        str(folder / "agent-result.json"),
        "--model",
        "gpt",
        "-",
    ]


def test_codex_resume_passes_the_sandbox_as_config(tmp_path):
    argv = Codex().argv(call(tmp_path, resume="thread-1"))
    assert argv[1:6] == ["exec", "resume", "--json", "-c", 'approval_policy="never"']
    assert 'sandbox_mode="read-only"' in argv and "-C" not in argv
    assert argv[-2:] == ["thread-1", "-"]


def test_claude_reply_counts_cached_input_and_names_the_billed_model(tmp_path):
    (tmp_path / "stdout.log").write_text(
        json.dumps(
            {
                "session_id": "s",
                "structured_output": {"outcome": "done"},
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                },
                "modelUsage": {"small": {"outputTokens": 1}, "large": {"outputTokens": 50}},
                "total_cost_usd": 0.25,
            }
        ),
        encoding="utf-8",
    )
    reply = Claude().reply(tmp_path)
    assert reply.document == {"outcome": "done"} and reply.session == "s"
    assert reply.usage == Usage(111, 2, 10, 100)
    assert reply.model == "large" and reply.reported_cost == 0.25


def test_claude_reply_without_usage_is_unknown_not_zero(tmp_path):
    (tmp_path / "stdout.log").write_text(
        json.dumps({"structured_output": {"outcome": "done"}}), encoding="utf-8"
    )
    reply = Claude().reply(tmp_path)
    assert reply.usage == Usage() and reply.model is None


@pytest.mark.parametrize(
    ("envelope", "error"),
    [
        ({"is_error": True}, ReportedError),
        ({"structured_output": "text"}, ValueError),
    ],
)
def test_claude_reply_fails_closed(tmp_path, envelope, error):
    (tmp_path / "stdout.log").write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(error):
        Claude().reply(tmp_path)


def test_codex_reply_reads_the_thread_and_the_last_turn_usage(tmp_path):
    (tmp_path / "agent-result.json").write_text('{"outcome": "done"}', encoding="utf-8")
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed"},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
    ]
    (tmp_path / "stdout.log").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    reply = Codex().reply(tmp_path)
    assert reply.session == "t-1" and reply.usage == Usage(7, 3, 0)


def test_a_reported_error_becomes_a_classified_failure(tmp_path):
    handler = CliHandler("claude", sys.executable)
    work = packet(tmp_path)
    folder = Path(work.directory)
    (folder / "stdout.log").write_text('{"is_error": true}', encoding="utf-8")
    (folder / "stderr.log").write_text("Error: 429 Too Many Requests", encoding="utf-8")
    result = handler.collect(work, 0, "rev")
    assert result.outcome == "waiting" and result.reason.startswith("claude reported an error")
    assert json.loads(result.data)["failure"] == "rate_limit"


@pytest.mark.parametrize(
    ("kind", "output"),
    [
        ("usage_limit", "You've hit your usage limit"),
        ("rate_limit", "rate limit exceeded"),
        ("unreachable", "network error"),
    ],
)
def test_recognised_limits_wait_their_cooldown(tmp_path, kind, output):
    work = packet(tmp_path)
    (Path(work.directory) / "stderr.log").write_text(output, encoding="utf-8")
    before = time.time()
    result = CliHandler("codex", sys.executable).collect(work, 1, "rev")
    assert result.outcome == "waiting" and kind.replace("_", " ") in result.reason
    assert result.resume_at is not None
    assert before + WAITS[kind] <= result.resume_at <= time.time() + WAITS[kind]


def test_a_stated_reset_is_capped_inside_the_engine_bound(tmp_path):
    work = packet(tmp_path)
    (Path(work.directory) / "stderr.log").write_text(
        "usage limit reached, try again in 30 days", encoding="utf-8"
    )
    result = CliHandler("codex", sys.executable).collect(work, 1, "rev")
    assert result.resume_at is not None and result.resume_at <= time.time() + MAX_WAIT


def test_an_authentication_failure_blocks_with_a_login_hint(tmp_path):
    handler = CliHandler("codex", sys.executable)
    work = packet(tmp_path)
    (Path(work.directory) / "stderr.log").write_text("Not logged in", encoding="utf-8")
    result = handler.collect(work, 1, "rev")
    assert result.outcome == "blocked" and "run the CLI login" in result.reason


def test_an_unrecognised_failure_points_at_stderr(tmp_path):
    result = CliHandler("codex", sys.executable).collect(packet(tmp_path), 3, "rev")
    assert result.outcome == "blocked"
    assert result.reason == "codex exit 3; other: inspect stderr.log"


def test_the_handler_model_names_the_attempt_when_the_provider_does_not(tmp_path):
    work = packet(tmp_path)
    folder = Path(work.directory)
    (folder / "agent-result.json").write_text(
        json.dumps({"outcome": "done", "reason": "ok", "standards": True, "specification": True}),
        encoding="utf-8",
    )
    (folder / "stdout.log").write_text("", encoding="utf-8")
    result = CliHandler("codex", sys.executable, "gpt").collect(work, 0, "rev")
    assert json.loads(result.data) == {"model": "gpt"}
