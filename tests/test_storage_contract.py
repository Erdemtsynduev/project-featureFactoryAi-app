"""Every persistence role behaves the same on SQLite and in memory.

One scenario per role of `sdd_core.ports.UnitOfWork` and `StateStore`, run against
both backends through the `any_store` fixture, so a rule written for one backend
cannot silently diverge in the other.
"""

import pytest
from sdd_core import machine
from sdd_core.models import Result, Run, Step, Workflow
from sdd_core.ports import Conflict, StaleVersion
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace

FLOW = Workflow(
    "contract",
    "work",
    (
        Step("work", "operation", "fake", transitions=(("done", "finish"),)),
        Step("finish", "finish"),
    ),
)


@pytest.fixture
def engine(any_store, tmp_path):
    return ApplicationEngine(any_store, GitProject(), LocalWorkspace()), tmp_path


def create(engine, run_id, root, dependencies=(), context="brief"):
    definition = engine.store.publish(FLOW)
    return engine.create(run_id, definition, root, context, "rev", 1, dependencies)


def test_create_is_idempotent_and_refuses_reuse_with_other_input(engine):
    engine, root = engine
    first = create(engine, "a", root)
    assert create(engine, "a", root) == first
    with pytest.raises(Conflict):
        create(engine, "a", root, context="other")
    with pytest.raises((ValueError, KeyError)):
        create(engine, "b", root, dependencies=("missing",))
    with pytest.raises(KeyError):
        engine.store.create(Run("c", "unknown", "work", "rev"), str(root), "", str(root), 1)
    assert engine.store.workflow(first.workflow_digest).id == "contract"
    assert engine.store.get("a") == first


def test_apply_is_compare_and_swap(engine):
    engine, root = engine
    create(engine, "a", root)
    stale = engine.store.get("a")
    with engine.store.unit() as unit:
        unit.apply(stale, machine.control(stale, "resume", 2))
    with pytest.raises(StaleVersion), engine.store.unit() as unit:
        unit.apply(stale, machine.control(stale, "resume", 3))
    with engine.store.unit() as unit:
        assert unit.run("a").version == stale.version + 1
        assert [run.id for run in unit.runs()] == ["a"]
        assert unit.last_transition() == 2


def test_context_location_and_relocation(engine):
    engine, root = engine
    create(engine, "a", root)
    with engine.store.unit() as unit:
        assert unit.context("a") == "brief"
        unit.set_context("a", "revised")
        workspace, claim = unit.location("a")
        assert dict(unit.locations())["a"] == workspace
        unit.relocate("a", workspace + "-lane", claim)
    with engine.store.unit() as unit:
        assert unit.context("a") == "revised"
        assert unit.location("a").workspace == workspace + "-lane"


def test_dependencies_order_runnable_and_can_be_replaced(engine):
    engine, root = engine
    create(engine, "a", root)
    create(engine, "b", root, dependencies=("a",))
    create(engine, "c", root)
    for key in ("a", "b", "c"):
        engine.command(key, "resume", key + "-go", engine.store.get(key).version, 2)
    with engine.store.unit() as unit:
        assert set(unit.dependency_edges()) == {("b", "a")}
        assert [run.id for run in unit.dependencies("b")] == ["a"]
        assert set(unit.runnable()) == {"a", "c"}, "b waits for a"
        unit.set_dependencies("b", ("c",))
    with engine.store.unit() as unit:
        assert set(unit.dependency_edges()) == {("b", "c")}


def test_commands_are_logged_by_request_id(engine):
    engine, root = engine
    create(engine, "a", root)
    with engine.store.unit() as unit:
        assert unit.command("req") is None
        unit.save_command("req", "request", "response")
    with engine.store.unit() as unit:
        assert unit.command("req") == ("request", "response")


def test_results_receipts_and_bases(engine):
    engine, root = engine
    create(engine, "a", root)
    engine.command("a", "resume", "go", engine.store.get("a").version, 2)
    run = engine.dispatch("a", 3, "att")
    result = Result("att", run.generation, "done", "ok", run.revision)
    engine.complete("a", result, 4)
    with engine.store.unit() as unit:
        assert unit.results("a") == (result,)
        assert len(unit.recent_results("a", 5)) == 1
        assert [step for step, _ in unit.step_results("a")] == ["work"]
        assert unit.attempt_bases("a") == (("work", run.revision),)
        assert unit.receipt("att") is not None and unit.receipt("other") is None
        assert unit.effect("att").status == "done"
        assert [effect.id for effect in unit.effects(("done",))] == ["att"]


def test_claims_of_started_unfinished_runs(engine):
    engine, root = engine
    create(engine, "a", root)
    create(engine, "b", root)
    engine.command("a", "resume", "go", engine.store.get("a").version, 2)
    engine.dispatch("a", 3, "att")
    with engine.store.unit() as unit:
        ((kind, claim),) = unit.active_claims()
        assert kind == "operation" and claim == unit.location("a").claim, "(step kind, claim)"
        assert [run.id for run, _ in unit.unfinished_claims("b")] == ["a"]
        assert unit.unfinished_claims("a") == ()


def test_policy_portfolio_and_lane_documents(engine):
    engine, root = engine
    create(engine, "a", root)
    with engine.store.unit() as unit:
        unit.set_policy(str(root), ("review", "check", "review"))
        unit.bind_portfolio("p", "doc")
        unit.save_lane("a", "lane-doc")
    with engine.store.unit() as unit:
        assert unit.policy(str(root)) == ("check", "review")
        assert unit.portfolio("p") == "doc" and unit.portfolio("q") is None
        with pytest.raises(Conflict):
            unit.bind_portfolio("p", "changed")
        assert unit.lane("a") == "lane-doc" and unit.lane("b") is None


def test_discard_removes_only_never_started_runs_all_or_none(engine):
    engine, root = engine
    create(engine, "a", root)
    create(engine, "b", root, dependencies=("a",))
    create(engine, "c", root)
    engine.command("c", "resume", "go", engine.store.get("c").version, 2)
    engine.dispatch("c", 3, "att")
    with pytest.raises(ValueError):
        engine.store.discard(("a",))  # b depends on a
    with pytest.raises(ValueError):
        engine.store.discard(("b", "c"))  # c started: nothing is removed
    assert engine.store.get("b").id == "b"
    assert engine.store.discard(("a", "b")) == ("a", "b")
    with engine.store.unit() as unit:
        assert [run.id for run in unit.runs()] == ["c"]
        assert unit.dependency_edges() == ()


def test_discarded_runs_take_their_catalog_records_with_them(engine):
    engine, root = engine
    create(engine, "a", root)
    create(engine, "b", root)
    catalog = engine.store.catalog()
    for key in ("a", "b"):
        catalog.save_task(key, "{}")
        catalog.save_artifact(key, "specification", "{}")
    engine.store.discard(("a",))
    assert [key for key, _ in catalog.tasks()] == ["b"]
    assert catalog.artifacts("a") == () and catalog.artifacts("b") != ()
