"""Application service guards, on both storage backends.

Most fixes in the tmux-driven predecessor were about stale or repeated inputs:
results arriving after a redispatch, duplicate completions, replayed operator
commands. Here each of them is refused or absorbed by construction.
"""

import pytest
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import revision
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_storage.memory import MemoryStore

FLOW = Workflow(
    "guards",
    "work",
    (
        Step("work", "agent", "fake", transitions=(("done", "ask"),), mutates=True),
        Step(
            "ask",
            "human",
            transitions=(("approved", "finish"),),
            config='{"questions":[{"id":"mode","question":"Which?","options":["a","b"],'
            '"recommended":"a"}]}',
        ),
        Step("finish", "finish"),
    ),
    max_input_chars=2000,
)


@pytest.fixture
def engine(any_store, tmp_path):
    store = any_store
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    root = tmp_path / "project"
    root.mkdir()
    engine.root = root  # type: ignore[attr-defined]
    engine.create("one", store.publish(FLOW), root, "Context", revision(root), 0)
    return engine


def started(engine: ApplicationEngine, attempt: str = "a1", now: float = 2) -> None:
    run = engine.store.get("one")
    if run.paused:
        engine.command("one", "resume", f"resume-{attempt}", run.version, now - 1)
    engine.dispatch("one", now, attempt)


def done(engine: ApplicationEngine, attempt: str = "a1", generation: int = 1) -> Result:
    return Result(attempt, generation, "done", "ok", engine.store.get("one").revision)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_queue_calls": -1}, "cannot be negative"),
        ({"max_agents": 0}, "At least one agent"),
        ({"max_operations": 0}, "At least one agent"),
    ],
)
def test_engine_refuses_impossible_budgets(options, message):
    with pytest.raises(ValueError, match=message):
        ApplicationEngine(MemoryStore(), GitProject(), LocalWorkspace(), **options)


def test_create_refuses_bad_ids_and_oversized_context(engine):
    digest = engine.store.get("one").workflow_digest
    with pytest.raises(ValueError, match="Invalid run id"):
        engine.create("no spaces", digest, engine.root, "", "r", 0)
    with pytest.raises(ValueError, match="character budget"):
        engine.create("two", digest, engine.root, "x" * 2001, "r", 0)


def test_a_replayed_command_returns_its_first_response(engine):
    first = engine.command("one", "resume", "req-1", 0, 1)
    assert engine.command("one", "resume", "req-1", 0, 2) == first
    with pytest.raises(Conflict, match="Command id reused"):
        engine.command("one", "pause", "req-1", 0, 2)
    with pytest.raises(Conflict, match="Stale command version"):
        engine.command("one", "pause", "req-2", 0, 2)


def test_a_repeated_result_is_absorbed_and_a_different_one_refused(engine):
    started(engine)
    applied = engine.complete("one", done(engine), 3)
    assert engine.complete("one", done(engine), 4) == applied
    other = Result("a1", 1, "done", "another story", applied.revision)
    with pytest.raises(Conflict, match="Conflicting repeated result"):
        engine.complete("one", other, 4)


def test_a_result_of_an_earlier_attempt_never_lands_on_the_new_one(engine):
    started(engine)
    engine.recover("one", 3, True, "lost", engine.store.get("one").revision)
    started(engine, "a2", now=100)
    with pytest.raises(ValueError, match="Stale attempt"):
        engine.complete("one", done(engine, "a1", 1), 101)
    assert engine.store.get("one").active.id == "a2"  # type: ignore[union-attr]


def test_answers_are_bound_to_the_version_and_the_questions_asked(engine):
    with pytest.raises(ValueError, match="Not waiting for a human"):
        engine.answer("one", "approved", "", {}, 0, 1)
    started(engine)
    engine.complete("one", done(engine), 3)
    engine.dispatch("one", 4, "h1")
    version = engine.store.get("one").version
    with pytest.raises(Conflict, match="Stale answer"):
        engine.answer("one", "approved", "", {}, version - 1, 5)
    with pytest.raises(ValueError, match="unknown question"):
        engine.answer("one", "approved", "", {"colour": "red"}, version, 5)
    answered = engine.answer("one", "approved", "", {"mode": "b"}, version, 5)
    assert answered.step == "finish" and answered.active is None


def test_operator_guidance_must_fit_and_say_something(engine):
    with pytest.raises(ValueError, match="cannot be empty"):
        engine.message("one", "   ", "m1", 0, 1)
    with pytest.raises(ValueError, match="exceeds context budget"):
        engine.message("one", "x" * 1500, "m2", 0, 1)


def test_recovery_refuses_live_or_stale_requests_and_workflows_without_a_path(engine):
    with pytest.raises(Conflict, match="Stale recovery"):
        engine.request_recovery("one", 99, 1)
    started(engine)
    run = engine.store.get("one")
    with pytest.raises(ValueError, match="inactive unfinished"):
        engine.request_recovery("one", run.version, 3)
    blocked = engine.recover("one", 3, False, "unknown host", run.revision)
    assert blocked.status == "blocked" and blocked.active is not None
    engine.complete("one", done(engine), 4)
    engine.block("one", 5, "operator")
    idle = engine.store.get("one")
    with pytest.raises(ValueError, match="no read-only recovery path"):
        engine.request_recovery("one", idle.version, 6)


def test_releasing_a_condition_needs_a_persisted_condition_attempt(engine):
    with pytest.raises(ValueError, match="No persisted condition"):
        engine.release_condition("one", 1)


def test_facts_of_a_run_without_results_are_empty(engine):
    assert engine.facts("one") == "{}"
    assert engine.asked("one") == "{}"
