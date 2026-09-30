"""The factory's board decisions as pure functions (see sdd_factory.board)."""

from dataclasses import replace

from sdd_core.admission import MAX_REVIVALS, REVIVE_AFTER, revivable
from sdd_core.models import Run
from sdd_factory.board import (
    Replan,
    below,
    live_tickets,
    replan,
    review_trigger,
    source_tickets,
)
from sdd_factory.model import TaskRecord

SOURCE = "plans/110_RALLY.md"


def run(identifier: str, **fields: object) -> Run:
    return replace(Run(identifier, "flow", "work", "rev"), **fields)


def record(**fields: object) -> TaskRecord:
    return TaskRecord(project="p", source=SOURCE, **fields)  # type: ignore[arg-type]


def test_replan_removes_never_started_sourced_work_and_reopens_its_parents():
    records = {
        "d": record(kind="draft", rows=("R1", "R2")),
        "f": record(kind="feature", parent="d", rows=("R1",)),
        "g": record(kind="feature", parent="d", rows=("R2",)),
        "t1": record(kind="ticket", parent="f"),
        "t2": record(kind="ticket", parent="f"),
        "r": record(kind="task", reviews="f"),
        "manual": TaskRecord(project="p", kind="ticket"),
        "idea": TaskRecord(project="p", kind="feature"),
    }
    runs = {
        "d": run("d", status="accepted", generation=2),
        "f": run("f", status="accepted", generation=2),
        "g": run("g"),
        "t1": run("t1"),
        "t2": run("t2", generation=1),
        "r": run("r"),
        "manual": run("manual"),
        "idea": run("idea"),
    }
    decided = replan(records, runs, [("t2", "t1")], "p")
    assert decided == Replan(frozenset({"g"}), frozenset({"d"})), "started t2 needs t1: t1 stays"
    decided = replan(records, runs, [], "p")
    assert decided.removed == {"g", "t1", "r"} and decided.reopened == {"d", "f"}
    assert replan(records, runs, [], "p", {"plans/111.md"}).removed == frozenset()
    assert replan(records, runs, [], "p", {SOURCE}).removed == {"g", "t1", "r"}
    assert "idea" not in decided.removed, "work a person typed has no source to take in again"


def test_a_review_follows_an_agent_block_or_an_exhausted_loop_only():
    stopped = run("t", status="blocked", reason="x", previous_attempt="a")
    assert review_trigger(replace(stopped, cause="visit_limit"), False)[0] == "limit"
    assert review_trigger(replace(stopped, cause="blocked"), True)[0] == "blocked"
    assert review_trigger(replace(stopped, cause="blocked"), False) is None
    assert review_trigger(replace(stopped, cause="workspace_changed"), True) is None


def test_new_tickets_may_wait_only_for_live_tickets_from_their_source():
    records = {
        "a": record(kind="ticket"),
        "b": record(kind="ticket", superseded="f2"),
        "c": TaskRecord(project="p", source="plans/111.md", kind="ticket"),
        "f": record(kind="feature"),
    }
    assert source_tickets(records, ["a", "b", "c", "f"], SOURCE) == {"a", "b"}, "a brief lists both"
    assert live_tickets(records, ["a", "b", "c", "f"], SOURCE) == {"a"}
    assert source_tickets(records, ["a"], "") == set()


def test_below_is_everything_cut_from_a_task():
    records = {
        "d": record(kind="draft"),
        "f": record(kind="feature", parent="d"),
        "t": record(kind="ticket", parent="f"),
        "other": record(kind="feature"),
    }
    assert below(records, "d") == {"f", "t"} and below(records, "t") == set()


def test_revival_waits_for_rest_and_counts():
    stuck = run("t", status="blocked", cause="wait_limit", reason="waits")
    assert revivable(stuck, 0, REVIVE_AFTER, 0, True)
    assert not revivable(stuck, 0, REVIVE_AFTER - 1, 0, True)
    assert not revivable(stuck, 0, REVIVE_AFTER, MAX_REVIVALS, True)
    assert not revivable(stuck, 0, REVIVE_AFTER, 0, False)
    assert not revivable(replace(stuck, cause="blocked"), 0, REVIVE_AFTER, 0, True)
