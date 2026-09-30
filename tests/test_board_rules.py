"""The factory's board decisions as pure functions (see sdd_factory.board)."""

from dataclasses import replace

from sdd_core.admission import MAX_REVIVALS, REVIVE_AFTER, revivable
from sdd_core.models import Run
from sdd_factory.board import Replan, plan_tickets, replan, review_trigger
from sdd_factory.model import TaskRecord


def run(identifier: str, **fields: object) -> Run:
    return replace(Run(identifier, "flow", "work", "rev"), **fields)


def record(**fields: object) -> TaskRecord:
    return TaskRecord(project="p", plan="110", **fields)  # type: ignore[arg-type]


def test_replan_removes_never_started_plan_work_and_reopens_its_feature():
    records = {
        "f": record(kind="feature", rows=("R1",)),
        "t1": record(kind="ticket", parent="f"),
        "t2": record(kind="ticket", parent="f"),
        "r": record(kind="task", reviews="f"),
        "manual": TaskRecord(project="p", kind="ticket"),
    }
    runs = {
        "f": run("f", status="accepted", generation=2),
        "t1": run("t1"),
        "t2": run("t2", generation=1),
        "r": run("r"),
        "manual": run("manual"),
    }
    decided = replan(records, runs, [("t2", "t1")], "p")
    assert decided == Replan(frozenset(), frozenset()), "started t2 needs t1: nothing moves"
    decided = replan(records, runs, [], "p")
    assert decided.removed == {"t1", "r"} and decided.reopened == {"f"}
    assert replan(records, runs, [], "p", plan="111").removed == frozenset()


def test_a_review_follows_an_agent_block_or_an_exhausted_loop_only():
    stopped = run("t", status="blocked", reason="x", previous_attempt="a")
    assert review_trigger(replace(stopped, cause="visit_limit"), False)[0] == "limit"
    assert review_trigger(replace(stopped, cause="blocked"), True)[0] == "blocked"
    assert review_trigger(replace(stopped, cause="blocked"), False) is None
    assert review_trigger(replace(stopped, cause="workspace_changed"), True) is None


def test_new_tickets_may_wait_only_for_live_tickets_of_their_plan():
    records = {
        "a": record(kind="ticket"),
        "b": record(kind="ticket", superseded="f2"),
        "c": TaskRecord(project="p", plan="111", kind="ticket"),
    }
    assert plan_tickets(records, ["a", "b", "c"], "110") == {"a"}
    assert plan_tickets(records, ["a"], "") == set()


def test_revival_waits_for_rest_and_counts():
    stuck = run("t", status="blocked", cause="wait_limit", reason="waits")
    assert revivable(stuck, 0, REVIVE_AFTER, 0, True)
    assert not revivable(stuck, 0, REVIVE_AFTER - 1, 0, True)
    assert not revivable(stuck, 0, REVIVE_AFTER, MAX_REVIVALS, True)
    assert not revivable(stuck, 0, REVIVE_AFTER, 0, False)
    assert not revivable(replace(stuck, cause="blocked"), 0, REVIVE_AFTER, 0, True)
