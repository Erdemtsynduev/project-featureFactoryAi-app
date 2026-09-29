"""Structured questions, recommended auto-answers and session continuation."""

import json
import sys
from pathlib import Path

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Attempt, Result, Step, Workflow
from sdd_core.questions import answer, questions, recommended
from sdd_core.sdk import Launch, Manifest, Packet, Registry
from sdd_providers.handlers import CliHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_storage.store import Store

ASKED = canonical(
    {
        "questions": [
            {
                "id": "mode",
                "question": "Which mode?",
                "options": ["Approval first", "Autonomous"],
                "recommended": "Approval first",
            },
            {
                "id": "scope",
                "question": "Include docs?",
                "options": ["Yes", "No"],
                "recommended": "No",
            },
        ]
    }
)


def test_questions_validate_and_compose_answers():
    asked = questions(ASKED)
    assert [q.id for q in asked] == ["mode", "scope"]
    reply, data = answer(asked, {"mode": "Autonomous"}, "Keep it small")
    assert "Which mode?\n→ Autonomous" in reply and reply.endswith("Keep it small")
    assert json.loads(data)["choices"] == {"mode": "Autonomous"}
    text, auto = recommended(ASKED)
    assert "→ Approval first" in text and "→ No" in text and json.loads(auto)["auto"] is True
    with pytest.raises(ValueError):
        answer(asked, {}, "  ")
    bad = json.loads(ASKED)
    bad["questions"][0]["recommended"] = "Something else"
    with pytest.raises(ValueError, match="one of the options"):
        questions(canonical(bad))
    partial = json.loads(ASKED)
    partial["questions"][1]["recommended"] = ""
    assert recommended(canonical(partial)) is None
    assert recommended("{}") is None


class Fake:
    manifest = Manifest("fake", "1", capabilities=("process", "agent", "resume"))

    def prepare(self, packet: Packet) -> Launch:
        return Launch((sys.executable, "-c", "pass"), packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        raise AssertionError("not executed")


def interview() -> Workflow:
    return Workflow(
        "interview",
        "ask",
        (
            Step("ask", "agent", "fake", transitions=(("questions", "answer"), ("done", "finish"))),
            Step("answer", "human", prompt="Answer", transitions=(("answered", "ask"),)),
            Step("finish", "finish"),
        ),
    )


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "state.db")
    engine = Engine(store)
    workspace = tmp_path / "work"
    workspace.mkdir()
    engine.create("one", store.publish(interview()), workspace, "Task", "rev", 0)
    engine.command("one", "resume", "r", 0, 1)
    run = engine.dispatch("one", 2, "ask-1")
    engine.complete(
        "one",
        Result(
            "ask-1",
            run.generation,
            "questions",
            "Two decisions",
            "rev",
            data=canonical(
                {
                    **json.loads(ASKED),
                    "session": {"id": "session-1", "step": "ask", "handler": "fake"},
                }
            ),
        ),
        3,
    )
    engine.dispatch("one", 4, "answer-1")
    return engine


def test_auto_answer_requires_the_operator_switch(engine):
    assert engine.auto_answer("one", 5) is None
    run = engine.store.get("one")
    run = engine.command("one", "auto", "auto-1", run.version, 5)
    assert run.auto_answer
    routed = engine.auto_answer("one", 6)
    assert routed is not None and routed.step == "ask" and routed.active is None
    with engine.store.unit() as unit:
        last = next(r for r in unit.results("one") if r.attempt_id == "answer-1")
    assert json.loads(last.data)["auto"] is True and "Approval first" in last.reason


def test_human_answer_by_choice_and_stale_version(engine):
    run = engine.store.get("one")
    with pytest.raises(ValueError, match="unknown question"):
        engine.answer("one", "answered", "", {"other": "x"}, run.version, 5)
    done = engine.answer(
        "one", "answered", "Also add tests", {"mode": "Autonomous"}, run.version, 5
    )
    assert done.step == "ask"


def test_continuation_resumes_the_same_session_with_only_the_answers(engine):
    run = engine.store.get("one")
    engine.answer("one", "answered", "", {"mode": "Autonomous", "scope": "Yes"}, run.version, 5)
    engine.dispatch("one", 6, "ask-2")
    registry = Registry()
    registry.register(Fake())
    coordinator = Coordinator(engine, registry)
    try:
        packet = coordinator.packet("one")
        assert packet.resume == "session-1"
        assert packet.context.startswith("Answers to your questions")
        assert "→ Autonomous" in packet.context and "Task" not in packet.context
    finally:
        coordinator.close()


def attempt_packet(tmp_path: Path, resume: str = "", mutates: bool = False) -> Packet:
    step = Step(
        "ask", "agent", "claude", "Ask", (("questions", "a"), ("done", "b")), mutates=mutates
    )
    folder = tmp_path / "attempt"
    folder.mkdir(exist_ok=True)
    return Packet(
        "r", Attempt("a1", "ask", 1, 0, 10, "rev"), step, str(tmp_path), str(folder), "New", resume
    )


def test_cli_handlers_continue_sessions_and_record_them(tmp_path):
    claude = CliHandler("claude", sys.executable, "opus")
    launch = claude.prepare(attempt_packet(tmp_path, "sess-9"))
    argv = launch.argv
    assert argv[argv.index("--resume") + 1] == "sess-9"
    # Prompts travel on stdin: a long brief must never reach the command line.
    assert argv[argv.index("-p") + 1] == "--output-format"
    assert launch.input.startswith("Continue the same bounded step")
    assert "--allowedTools" not in argv, "a read-only step runs no commands"
    working = claude.prepare(attempt_packet(tmp_path, mutates=True)).argv
    assert working[working.index("--allowedTools") + 1] == "Bash,PowerShell"
    assert working[working.index("--disallowedTools") + 1] == "Agent,Task"
    codex = CliHandler("codex", sys.executable, "gpt")
    launch = codex.prepare(attempt_packet(tmp_path, "thread-3", mutates=True))
    argv = launch.argv
    assert list(argv[1:3]) == ["exec", "resume"] and list(argv[-2:]) == ["thread-3", "-"]
    assert 'sandbox_mode="workspace-write"' in argv
    assert launch.input.startswith("Continue the same bounded step")
    fresh = codex.prepare(attempt_packet(tmp_path))
    assert "resume" not in fresh.argv and "--sandbox" in fresh.argv
    assert fresh.argv[-1] == "-" and "New" in fresh.input
    # Agents plan within the attempt's bound; a resumed session gets a fresh one.
    assert "Time budget: the engine stops this step 1 min after" in fresh.input
    assert "Time budget:" in launch.input
    packet = attempt_packet(tmp_path)
    Path(packet.directory, "stdout.log").write_text(
        json.dumps(
            {
                "session_id": "sess-10",
                "structured_output": {
                    "outcome": "questions",
                    "reason": "Need a decision",
                    "standards": True,
                    "specification": True,
                    **json.loads(ASKED),
                },
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        ),
        encoding="utf-8",
    )
    result = claude.collect(packet, 0, "rev")
    data = json.loads(result.data)
    assert data["session"] == {"id": "sess-10", "step": "ask", "handler": "claude"}
    assert [q["id"] for q in data["questions"]] == ["mode", "scope"]
