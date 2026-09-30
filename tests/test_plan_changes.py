"""Plan review rules, ticket needs and per-step web access: pure and provider layers."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sdd_core.codec import canonical
from sdd_core.graph import validate
from sdd_core.models import Attempt, Step, Workflow
from sdd_core.options import StepOptions
from sdd_core.plan_changes import (
    MAX_REVIEWS,
    Cancel,
    Guide,
    Merge,
    Need,
    PlanSnapshot,
    Revise,
    Split,
    TicketState,
    plan_changes_of,
    review_due,
    revise,
)
from sdd_core.sdk import Packet
from sdd_core.tickets import TicketDraft, needs_of, tickets_of
from sdd_providers.dialects import Claude, Codex, Invocation
from sdd_providers.handlers import PRODUCT_OUTPUTS, instructions, result_schema, shared_data
from sdd_workflows.templates import capabilities, plan_review, ticket, with_tools
from sdd_workflows.templates import draft as draft_flow


def draft(identifier: str, *depends_on: str, **fields: object) -> TicketDraft:
    return TicketDraft(identifier, identifier.upper(), depends_on=depends_on, **fields)  # type: ignore[arg-type]


PLAN = (
    draft("a"),
    draft("b", "a"),
    draft("c", "a"),
    draft("d", "b", "c"),
    draft("t10", "a"),
)
STATES = {
    "a": TicketState("r-a", "accepted", True),
    "t10": TicketState("r-t10", "blocked", True, "no licensed recordings"),
}
SNAPSHOT = PlanSnapshot(PLAN, STATES, "## Acceptance\nAC-1 engine loops")


# Ticket needs ---------------------------------------------------------------------------


def test_needs_are_read_and_the_hitl_prefix_still_means_a_person():
    assert needs_of({"needs": ["asset", "web", "asset"]}) == ("asset", "web")
    assert needs_of({"goal": "HITL: pick the art style"}) == ("human",)
    assert needs_of({"goal": "Build it"}) == ()
    with pytest.raises(ValueError, match="Unknown ticket needs: robot"):
        needs_of({"needs": ["robot"]})


def test_breakdowns_carry_needs_and_mark_held_tickets():
    data = canonical({"tickets": [{"id": "T1", "title": "Loops", "needs": ["asset"]}]})
    (only,) = tickets_of(data)
    assert only.needs == ("asset",) and only.held
    assert "Needs: asset" in only.context("")


# Reading proposals ------------------------------------------------------------------------


def test_every_change_kind_is_read_from_the_agents_result():
    data = canonical(
        {
            "plan_changes": [
                {"kind": "revise", "drafts": [{"id": "b", "title": "B2"}]},
                {"kind": "merge", "tickets": ["b", "c"], "drafts": [{"id": "b", "title": "BC"}]},
                {
                    "kind": "split",
                    "ticket": "d",
                    "drafts": [{"id": "d1", "title": "D1"}, {"id": "d2", "title": "D2"}],
                },
                {"kind": "cancel", "ticket": "d"},
                {"kind": "need", "ticket": "t10", "needs": ["asset"]},
                {"kind": "guide", "ticket": "t10", "text": "Use the bundled loops", "retry": True},
            ]
        }
    )
    kinds = [type(change).__name__ for change in plan_changes_of(data)]
    assert kinds == ["Revise", "Merge", "Split", "Cancel", "Need", "Guide"]
    with pytest.raises(ValueError, match="Unknown plan change: rename"):
        plan_changes_of(canonical({"plan_changes": [{"kind": "rename"}]}))


# The revision -------------------------------------------------------------------------------


def test_merge_split_and_cancel_rewire_every_dependent():
    changes = (
        Merge(("b", "c"), draft("b", "a", paths=("libraries/audio",))),
        Split("d", (draft("d1", "b"), draft("d2", "d1"))),
    )
    result = revise(SNAPSHOT, changes)
    assert [(d.id, d.depends_on) for d in result.drafts] == [
        ("a", ()),
        ("b", ("a",)),
        ("t10", ("a",)),
        ("d1", ("b",)),
        ("d2", ("d1",)),
    ]
    assert [d.id for d in result.create] == ["d1", "d2"]
    assert [d.id for d in result.update] == ["b"]
    assert result.discard == ("c", "d")


def test_a_cancelled_ticket_hands_its_prerequisites_to_its_dependents():
    result = revise(SNAPSHOT, (Cancel("b"),))
    assert dict((d.id, d.depends_on) for d in result.drafts)["d"] == ("a", "c")
    assert [d.id for d in result.update] == ["d"]


def test_started_tickets_take_only_needs_and_guidance():
    for change in (Revise(draft("t10")), Cancel("t10"), Merge(("t10", "b"), draft("b"))):
        with pytest.raises(ValueError, match="has started"):
            revise(SNAPSHOT, (change,))
    result = revise(
        SNAPSHOT, (Need("t10", ("asset",)), Guide("t10", "Use synthetic loops meanwhile", True))
    )
    assert result.holds == ("t10",) and result.guidance[0].retry
    assert result.update == (), "a started ticket's brief is never rewritten"
    assert dict((d.id, d.needs) for d in result.drafts)["t10"] == ("asset",)


def test_finished_tickets_and_bad_plans_are_refused():
    with pytest.raises(ValueError, match="finished"):
        revise(SNAPSHOT, (Guide("a", "again"),))
    with pytest.raises(ValueError, match="Unknown ticket"):
        revise(SNAPSHOT, (Cancel("zz"),))
    with pytest.raises(ValueError, match="already used"):
        revise(SNAPSHOT, (Split("d", (draft("b"), draft("d2"))),))
    with pytest.raises(ValueError, match="changed twice"):
        revise(SNAPSHOT, (Revise(draft("d", "b")), Revise(draft("d", "c"))))
    with pytest.raises(ValueError, match="cyclic"):
        revise(SNAPSHOT, (Revise(draft("b", "d")),))


def test_the_review_brief_lists_every_ticket_within_its_budget():
    brief = SNAPSHOT.brief("ticket t10 blocked", "- implement: no internet", 4000)
    assert "Blocked: no licensed recordings" in brief and "t10: T10 · blocked" in brief
    assert "AC-1 engine loops" in brief
    short = SNAPSHOT.brief("x", "", len(SNAPSHOT.brief("x", "", 4000)) - 10)
    assert "[Specification shortened]" in short
    with pytest.raises(ValueError, match="budget"):
        SNAPSHOT.brief("x", "", 50)


def test_reviews_are_one_at_a_time_and_bounded():
    assert review_due(False, 0) and not review_due(True, 0)
    assert not review_due(False, MAX_REVIEWS)


# Step tools and the web -------------------------------------------------------------------


def test_step_tools_are_a_closed_vocabulary_for_agent_steps():
    assert StepOptions.parse('{"tools":["web"]}').tools == ("web",)
    with pytest.raises(ValueError, match="Unknown step tools: shell"):
        StepOptions.parse('{"tools":["shell"]}')
    flow = Workflow(
        "w",
        "check",
        (
            Step(
                "check",
                "check",
                "command",
                transitions=(("passed", "end"),),
                config='{"tools":["web"]}',
            ),
            Step("end", "finish"),
        ),
    )
    with pytest.raises(ValueError, match="Only an agent step can use tools"):
        validate(flow)


def invocation(tmp_path: Path, *, mutates: bool, web: bool) -> Invocation:
    folder = tmp_path / "attempt"
    folder.mkdir(exist_ok=True)
    config = '{"tools":["web"]}' if web else "{}"
    step = Step("work", "agent", "x", "Do it", (("done", "end"),), mutates=mutates, config=config)
    packet = Packet(
        "run", Attempt("a1", "work", 1, 0, 10, "rev"), step, str(tmp_path), str(folder), "C"
    )
    return Invocation("/bin/agent", None, packet, {"type": "object"})


@pytest.mark.parametrize(
    ("mutates", "web", "allowed"),
    [
        (False, False, None),
        (False, True, "WebSearch,WebFetch"),
        (True, False, "Bash,PowerShell"),
        (True, True, "Bash,PowerShell,WebSearch,WebFetch"),
    ],
)
def test_claude_allowed_tools_follow_the_step(tmp_path, mutates, web, allowed):
    argv = Claude().argv(invocation(tmp_path, mutates=mutates, web=web))
    found = argv[argv.index("--allowedTools") + 1] if "--allowedTools" in argv else None
    assert found == allowed
    if not mutates:
        assert "Bash" in argv[argv.index("--disallowedTools") + 1], (
            "reading steps never run commands"
        )


@pytest.mark.parametrize("resume", [None, "thread-1"])
def test_codex_turns_on_live_search_for_web_steps(tmp_path, resume):
    call = invocation(tmp_path, mutates=True, web=True)
    call = replace(call, packet=replace(call.packet, resume=resume or ""))
    argv = Codex().argv(call)
    assert 'web_search="live"' in argv
    plain = Codex().argv(invocation(tmp_path, mutates=True, web=False))
    assert 'web_search="live"' not in plain


def test_capabilities_are_derived_from_the_ticket_flow():
    plain = capabilities(ticket(()))
    assert "- implement: edits files and runs commands; has no internet." in plain
    assert "No agent can buy, license or download gated files" in plain
    webbed = capabilities(with_tools(ticket(()), ("web",)))
    assert "- implement: edits files and runs commands; has web access." in webbed
    validate(plan_review())


# Structured outputs per product ------------------------------------------------------------


def product_packet(tmp_path: Path, produces: str) -> Packet:
    step = Step(
        "plan",
        "agent",
        "x",
        transitions=(("done", "end"),),
        config=json.dumps({"produces": produces}),
    )
    return Packet(
        "run", Attempt("a1", "plan", 1, 0, 10, "rev"), step, str(tmp_path), str(tmp_path), "C"
    )


def test_each_product_declares_its_output_schema_and_instruction(tmp_path):
    for product, output in PRODUCT_OUTPUTS.items():
        packet = product_packet(tmp_path, product)
        schema = result_schema(packet)
        assert schema["properties"][output.key] == output.schema  # type: ignore[index]
        assert output.instruction in instructions(packet)
    tickets = PRODUCT_OUTPUTS["tickets"].schema["items"]  # type: ignore[index]
    assert "needs" in tickets["required"]  # type: ignore[index]
    features = PRODUCT_OUTPUTS["features"].schema["items"]  # type: ignore[index]
    assert features["required"] == ["id", "title", "goal", "covers", "depends_on", "labels"]  # type: ignore[index]
    plain = result_schema(product_packet(tmp_path, "specification"))
    assert "tickets" not in plain["properties"] and "plan_changes" not in plain["properties"]  # type: ignore[operator]


def test_a_groom_step_returns_features_and_a_bad_cut_is_refused(tmp_path):
    packet = product_packet(tmp_path, "features")
    cut = [{"id": "F1", "title": "Tiling", "covers": ["R1"], "labels": ["Shaders"]}]
    assert shared_data({"features": cut, "tickets": [{"id": "x"}]}, packet) == {"features": cut}
    with pytest.raises(ValueError, match="cyclic or unknown"):
        shared_data({"features": [{"id": "F1", "title": "A", "depends_on": ["F9"]}]}, packet)
    with pytest.raises(ValueError, match="At most 3 labels"):
        shared_data({"features": [{**cut[0], "labels": ["a", "b", "c", "d"]}]}, packet)
    validate(draft_flow())
    assert draft_flow().step("groom").options.produces == "features"
    assert not draft_flow().step("groom").mutates, "the lead only reads"


def test_a_step_keeps_only_its_own_product_and_checks_it(tmp_path):
    changes = {
        "plan_changes": [{"kind": "cancel", "ticket": "T2"}],
        "tickets": [{"id": "x", "title": "y"}],
    }
    kept = shared_data(changes, product_packet(tmp_path, "plan_changes"))
    assert set(kept) == {"plan_changes"}
    with pytest.raises(ValueError, match="Unknown plan change"):
        shared_data(
            {"plan_changes": [{"kind": "rename"}]}, product_packet(tmp_path, "plan_changes")
        )
