"""Bundled workflows are valid, and the exported schema matches the wire models."""

import pytest
from sdd_core.codec import workflow_json, workflow_load
from sdd_core.graph import validate
from sdd_core.models import Step, Workflow
from sdd_core.schema import workflow_schema
from sdd_workflows.templates import CheckCommand, approved_feature, interview, main_flow, ticket

CHECK = CheckCommand(("C:/Python/python.exe", "-m", "pytest"), timeout=120, title="pytest")


@pytest.mark.parametrize(
    "flow",
    [
        main_flow(),
        approved_feature(),
        interview(),
        ticket(()),
        ticket((CHECK,)),
        ticket((CHECK, CHECK, CHECK), allow_commits=True),
    ],
    ids=["main-flow", "feature", "interview", "ticket-0", "ticket-1", "ticket-3"],
)
def test_templates_validate_and_roundtrip(flow):
    validate(flow, ("review",) if any(s.id == "review" for s in flow.steps) else ())
    assert workflow_load(workflow_json(flow)) == flow


def test_schema_matches_wire_models():
    schema = workflow_schema()
    assert set(schema["properties"]) == set(Workflow.__dataclass_fields__)  # type: ignore[arg-type]
    step = schema["properties"]["steps"]["items"]  # type: ignore[index]
    assert set(step["properties"]) == set(Step.__dataclass_fields__)
    kinds = step["properties"]["kind"]["enum"]
    assert set(kinds) == {"agent", "check", "human", "condition", "operation", "finish"}


def test_ticket_checks_are_ordered_gates_that_share_one_repair_loop():
    flow = ticket((CHECK, CHECK))
    checks = [s for s in flow.steps if s.kind == "check"]
    assert [dict(s.transitions)["passed"] for s in checks] == ["check_2", "review"]
    assert all(
        s.gate and s.required and dict(s.transitions)["failed"] == "diagnose" for s in checks
    )
    assert dict(flow.step("repair").transitions)["done"] == "check_1"
    assert flow.max_planning_calls == 0 and checks[0].timeout == 120
