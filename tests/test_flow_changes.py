"""Changing the flow of work in progress: new versions and migration onto them."""

from dataclasses import replace

import pytest
from sdd_core import machine
from sdd_core.editor import Insert, SetOption, SetProfile, Skip, changed_flow, flow_changes_of
from sdd_core.models import Attempt, Event, Run, Step, Transition, Workflow
from sdd_core.storage_rules import check_transition

FLOW = Workflow(
    "ticket",
    "implement",
    (
        Step("implement", "agent", "fake", transitions=(("done", "check"),), mutates=True),
        Step(
            "check",
            "check",
            "command",
            transitions=(("passed", "review"), ("failed", "implement")),
            required=True,
            gate=True,
        ),
        Step(
            "review", "agent", "fake", transitions=(("passed", "finish"), ("failed", "implement"))
        ),
        Step("finish", "finish"),
    ),
)


def at(step: str, **fields: object) -> Run:
    run = Run("t", "old", step, "rev", paused=False, generation=3)
    return replace(
        run,
        visits=(("check", 1), ("implement", 2), ("review", 1)),
        completed=("check", "implement"),
        gates=(("check", "rev"),),
        **fields,
    )


def test_skipping_a_step_routes_around_it_and_says_where_its_work_went():
    flow, moved = changed_flow(FLOW, (Skip("review"),))
    assert moved == {"review": "finish"}
    assert "review" not in {step.id for step in flow.steps}
    assert flow.step("check").target("passed") == "finish"
    with pytest.raises(ValueError, match="needs a person's decision"):
        changed_flow(FLOW, (Skip("check"),))
    skipped, moved = changed_flow(FLOW, (Skip("check", required=True),))
    assert moved == {"check": "review"} and skipped.step("implement").target("done") == "review"
    with pytest.raises(ValueError, match="finish"):
        changed_flow(FLOW, (Skip("finish"),))


def test_profiles_options_and_inserted_steps_are_new_versions():
    flow, _ = changed_flow(
        FLOW,
        (
            SetProfile("review", "codex"),
            SetOption("check", "argv", ["python", "-m", "pytest"]),
            Insert("review", "passed", Step("docs", "agent", "fake")),
        ),
    )
    assert flow.step("review").profile == "codex"
    assert flow.step("check").options.argv == ("python", "-m", "pytest")
    assert flow.step("review").target("passed") == "docs"
    with pytest.raises(ValueError, match="Unknown step option"):
        changed_flow(FLOW, (SetOption("check", "nonsense", 1),))
    with pytest.raises(ValueError, match="Only an agent"):
        changed_flow(FLOW, (SetProfile("check", "codex"),))
    assert flow_changes_of([{"kind": "skip", "step": "review"}]) == (Skip("review"),)
    with pytest.raises(ValueError, match="Unknown flow change"):
        flow_changes_of([{"kind": "teleport"}])


def test_a_run_migrates_keeping_its_progress_where_steps_are_unchanged():
    skipped, moved = changed_flow(FLOW, (Skip("review"),))
    state = machine.migrate(at("review"), FLOW, skipped, "new", 5, moved).state
    assert (state.workflow_digest, state.step) == ("new", "finish")
    assert dict(state.visits) == {"check": 1, "implement": 2}
    assert state.gates == (("check", "rev"),), "an unchanged gate stays passed"
    configured, _ = changed_flow(FLOW, (SetOption("check", "argv", ["x"]),))
    again = machine.migrate(at("implement"), FLOW, configured, "new", 5).state
    assert again.gates == () and again.completed == ("check", "implement")


def test_migration_refuses_live_finished_or_misplaced_runs():
    skipped, _ = changed_flow(FLOW, (Skip("review"),))
    live = at("review", status="running", active=Attempt("a", "review", 3, 1, 2, "rev"))
    with pytest.raises(ValueError, match="live attempt"):
        machine.migrate(live, FLOW, skipped, "new", 5)
    with pytest.raises(ValueError, match="accepted"):
        machine.migrate(at("finish", status="accepted"), FLOW, skipped, "new", 5)
    with pytest.raises(ValueError, match="no place"):
        machine.migrate(at("review"), FLOW, skipped, "new", 5)


def test_storage_accepts_a_new_workflow_only_as_a_migration():
    before = at("implement")
    moved = replace(before, workflow_digest="new", version=1)
    with pytest.raises(ValueError, match="only by migration"):
        check_transition(before, Transition(moved, (Event("plan_revised", 1, ""),)))
    check_transition(before, Transition(moved, (Event("workflow_migrated", 1, "new"),)))


def test_a_started_run_changes_its_flow_through_the_engine(any_store, tmp_path):
    from sdd_core.models import Result
    from sdd_core.ports import Conflict
    from sdd_runtime.application import ApplicationEngine
    from sdd_runtime.git import GitProject
    from sdd_runtime.workspace import LocalWorkspace

    engine = ApplicationEngine(any_store, GitProject(), LocalWorkspace())
    run = engine.create("t", any_store.publish(FLOW), tmp_path, "brief", "rev", 1)
    engine.command("t", "resume", "go", run.version, 2)
    started = engine.dispatch("t", 3, "a1")
    with pytest.raises(ValueError, match="live attempt"):
        engine.change_flow("t", (Skip("review"),), "c0", started.version, 4)
    engine.complete("t", Result("a1", started.generation, "done", "ok", "rev"), 4)
    at_check = engine.store.get("t")
    changed = engine.change_flow("t", (Skip("review"),), "c1", at_check.version, 5)
    assert changed.workflow_digest != at_check.workflow_digest and changed.step == "check"
    assert engine.store.workflow(changed.workflow_digest).step("check").target("passed") == "finish"
    assert engine.change_flow("t", (Skip("review"),), "c1", at_check.version, 6) == changed
    with pytest.raises(Conflict, match="Stale"):
        engine.change_flow("t", (SetProfile("implement", "codex"),), "c2", at_check.version, 6)
    with engine.store.unit() as unit:
        assert unit.context("t") == "brief" and unit.run("t").visits == changed.visits
