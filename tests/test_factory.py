"""The factory's typed vocabulary: step options, task records, artifacts, per-repo checks."""

import sys

import pytest
from sdd_core.graph import validate
from sdd_core.models import Step, Workflow
from sdd_core.options import StepOptions
from sdd_factory.catalog import Artifact
from sdd_factory.model import TaskRecord
from sdd_ui.service import WorkspaceService
from sdd_workflows.templates import feature


def test_step_options_are_typed_and_unknown_options_are_refused_on_publish():
    options = StepOptions.parse('{"produces":"tickets","purpose":"planning"}')
    assert options.produces == "tickets" and options.planning
    assert StepOptions.parse(options.render()) == options
    assert StepOptions.parse("{}").cwd == "."
    with pytest.raises(ValueError, match="argv must be a list"):
        StepOptions.parse('{"argv":"python"}')
    with pytest.raises(ValueError, match="Unknown step product"):
        StepOptions.parse('{"produces":"poem"}')
    flow = Workflow(
        "x",
        "work",
        (
            Step(
                "work", "operation", "fake", transitions=(("done", "done"),), config='{"argvv":[]}'
            ),
            Step("done", "finish"),
        ),
    )
    with pytest.raises(ValueError, match="Unknown step options on work: argvv"):
        validate(flow)
    roles = {step.id: StepOptions.parse(step.config).produces for step in feature().steps}
    assert roles["spec"] == "specification" and roles["tickets"] == "tickets"


def test_task_records_read_their_kind_and_can_be_corrected(tmp_path):
    assert TaskRecord.load({"kind": "feature", "rows": ["A-1"]}).kind == "feature"
    assert TaskRecord.load({"kind": "strange"}).kind == "task"
    assert TaskRecord(title="T").document() == {
        "kind": "task",
        "language": "ru",
        "project": "",
        "title": "T",
    }
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "work"
        root.mkdir()
        flow = service.engine.store.publish(Workflow("x", "done", (Step("done", "finish"),)))
        service.engine.create("one", flow, root, "", "rev", 1)
        service.catalog.save_task("one", TaskRecord(title="First", kind="feature"))
        service.catalog.save_task("one", TaskRecord(title="Ignored"))  # creation is idempotent
        assert service.catalog.task("one").title == "First"
        renamed = service.mutate("rename", {"id": "one", "title": "Better title"})
        assert renamed["title"] == "Better title" and renamed["kind"] == "feature"
        with pytest.raises(KeyError):
            service.catalog.update_task("missing", TaskRecord())
        service.catalog.save_artifact(Artifact("one", "specification", "PRD"))
        service.catalog.save_artifact(Artifact("one", "specification", "PRD v2"))
        assert service.catalog.artifacts("one")["specification"].content == "PRD v2"
        with pytest.raises(ValueError, match="Unknown artifact kind"):
            service.catalog.save_artifact(Artifact("one", "poem", "x"))
    finally:
        service.coordinator.close()


def test_tickets_run_the_checks_of_the_repositories_they_own(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "game"
        (root / "libraries" / "terrain").mkdir(parents=True)
        python = sys.executable
        service.mutate(
            "project",
            {
                "id": "game",
                "name": "Game",
                "workspace": str(root),
                "checks": [python, "-c", "print('all')"],
                "repository_checks": {"libraries/terrain": [python, "-c", "print('terrain')"]},
            },
        )
        owned = service.flows.template("ticket", "game", "ru", ("libraries/terrain",))
        general = service.flows.template("ticket", "game", "ru")
        commands = lambda flow: [  # noqa: E731
            StepOptions.parse(s.config) for s in flow.steps if s.kind == "check"
        ]
        assert [(o.argv[-1], o.cwd) for o in commands(owned)] == [
            ("print('terrain')", "libraries/terrain")
        ]
        assert [o.argv[-1] for o in commands(general)] == ["print('all')"]
        with pytest.raises(ValueError, match="not a folder"):
            service.mutate(
                "project",
                {
                    "id": "game",
                    "name": "Game",
                    "workspace": str(root),
                    "repository_checks": {"missing": [python]},
                },
            )
    finally:
        service.coordinator.close()


def test_a_project_names_the_profile_of_each_role(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        root = tmp_path / "game"
        root.mkdir()
        project = {"id": "game", "name": "Game", "workspace": str(root)}
        roles = {"lead": "opus", "implementer": "kimi", "reviewer": ""}
        saved = service.mutate("project", {**project, "roles": roles})
        assert saved["roles"] == {"implementer": "kimi", "lead": "opus"}, (
            "unset roles keep defaults"
        )
        handlers = lambda name: {  # noqa: E731
            step.id: step.handler
            for step in service.flows.template(name, "game").steps
            if step.kind == "agent"
        }
        assert handlers("plan-review") == {"replan": "opus"}
        ticket = handlers("ticket")
        assert (ticket["implement"], ticket["repair"], ticket["review"]) == (
            "kimi",
            "kimi",
            "codex",
        )
        assert set(handlers("feature").values()) == {"codex"}, "the analyst was left as it was"
        assert service.flows.template("ticket") == service.flows.template("ticket", "missing")
        with pytest.raises(ValueError, match="Unknown roles: boss; known: lead, analyst"):
            service.mutate("project", {**project, "roles": {"boss": "opus"}})
        with pytest.raises(ValueError, match="Invalid profile names"):
            service.mutate("project", {**project, "roles": {"lead": "two words"}})
    finally:
        service.coordinator.close()
