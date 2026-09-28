"""Shared task memory, ticket drafts, check-less templates and the attention reason."""

import sys

import pytest
from sdd_core.codec import canonical, object_json, result_json
from sdd_core.machine import WAIT_RETRY_LIMIT
from sdd_core.memory import brief, fit, notes, tickets_of
from sdd_core.models import Attempt, Result, Run, Step
from sdd_ui.attention import attention
from sdd_workflows.templates import feature, main_flow, with_checks


def receipt(outcome: str, reason: str, **data: object) -> str:
    return result_json(Result("a", 1, outcome, reason, "r", data=canonical(data)))


def test_brief_deduplicates_notes_oldest_first_and_bounds_handoff():
    newest_first = (
        receipt("done", "third " * 500, notes=["Use UTC", "API lives in api/"]),
        receipt("failed", "second", notes=["Use UTC"]),
        receipt("done", "first", notes=["Keep v1 endpoint"]),
        receipt("done", "zeroth"),
    )
    memory = brief(newest_first)
    assert memory.notes == ("Keep v1 endpoint", "Use UTC", "API lives in api/")
    assert len(memory.handoff) == 3
    assert memory.handoff[0].startswith("- [done] third") and memory.handoff[0].endswith("…")
    assert "zeroth" not in memory.render()


def test_fit_keeps_context_and_newest_memory_within_budget():
    assert fit("ctx", "", 10) == "ctx"
    assert fit("ctx", "memory", 100) == "ctx\nmemory"
    long = "\n".join(f"line {i}" for i in range(200))
    fitted = fit("ctx", long, 400)
    assert fitted.startswith("ctx\n") and len(fitted) <= 400 and "line 199" in fitted


def test_notes_reject_non_text():
    assert notes(canonical({"notes": ["  a  ", ""]})) == ("a",)
    with pytest.raises(ValueError):
        notes(canonical({"notes": [1]}))


def test_ticket_drafts_are_ordered_and_validated():
    data = canonical(
        {
            "tickets": [
                {"id": "ui", "title": "Screen", "depends_on": ["api"], "acceptance": ["AC-1"]},
                {"id": "api", "title": "Endpoint", "paths": ["api/"]},
            ]
        }
    )
    drafts = tickets_of(data)
    assert [d.id for d in drafts] == ["api", "ui"]
    context = drafts[1].context("Spec text")
    assert "Ticket ui: Screen" in context and "- AC-1" in context and "Spec text" in context
    for broken in (
        [{"id": "a", "title": "A", "depends_on": ["missing"]}],
        [
            {"id": "a", "title": "A", "depends_on": ["b"]},
            {"id": "b", "title": "B", "depends_on": ["a"]},
        ],
        [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}],
        [{"id": "", "title": "A"}],
    ):
        with pytest.raises(ValueError):
            tickets_of(canonical({"tickets": broken}))


def test_requirement_breakdown_declares_structured_tickets():
    tickets = feature().step("tickets")
    assert '"produces":"tickets"' in tickets.config


def test_project_without_checks_bypasses_generic_check_steps():
    flow = with_checks(main_flow(), [])
    ids = {step.id for step in flow.steps}
    assert "checks" not in ids
    assert ("done", "review") in flow.step("implement").transitions
    assert ("done", "review") in flow.step("repair").transitions
    configured = with_checks(main_flow(), [sys.executable, "-m", "pytest"])
    assert object_json(configured.step("checks").config)["argv"][0] == sys.executable


def reason(run: Run, step: Step, **overrides: object):
    options = {
        "queue_running": True,
        "pending": (),
        "has_profile": lambda _: True,
        "available": lambda _: True,
    }
    options.update(overrides)
    return attention(run, step, **options)  # type: ignore[arg-type]


def test_attention_names_the_first_thing_to_fix():
    agent = Step("work", "agent", "claude", transitions=(("done", "end"),))
    human = Step("ask", "human", prompt="Pick", transitions=(("answered", "end"),))
    run = Run("one", "d", "work", "r")
    assert reason(run, agent).code == "paused"
    assert reason(run, agent, has_profile=lambda _: False).code == "no_profile"
    assert reason(run, agent, available=lambda _: False).code == "profile_resting"
    ready = Run("one", "d", "work", "r", paused=False)
    assert reason(ready, agent, pending=("dep",)).code == "dependencies"
    assert reason(ready, agent, queue_running=False).code == "queue_paused"
    assert reason(ready, agent).code == "queued"
    exhausted = Run("one", "d", "work", "r", status="blocked", reason=WAIT_RETRY_LIMIT)
    assert reason(exhausted, agent).code == "limits_exhausted"
    active = Run("one", "d", "ask", "r", active=Attempt("a", "ask", 1, 0, 1, "r"))
    assert reason(active, human).code == "answer"
    assert reason(Run("one", "d", "work", "r", status="accepted"), agent).tone == "done"
