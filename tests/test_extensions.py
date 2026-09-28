from dataclasses import replace

import pytest
from sdd_core.editor import insert_step, simulate
from sdd_core.models import Step
from sdd_core.sdk import Manifest, Registry
from sdd_runtime.plugins import load_extensions
from test_runtime import runtime


def test_external_installed_package_without_core_edits(tmp_path):
    coordinator = runtime(tmp_path)
    load_extensions(coordinator.registry, ("example",))
    workflow = coordinator.engine.store.workflow(
        coordinator.engine.store.get("one").workflow_digest
    )
    workflow = replace(
        workflow,
        steps=tuple(
            replace(s, handler="example") if s.kind == "check" else s for s in workflow.steps
        ),
    )
    definition = coordinator.engine.store.publish(workflow)
    # New run pins a new definition; old run remains unchanged.
    old = coordinator.engine.store.get("one")
    coordinator.engine.command("one", "pause", "pause", old.version, 1)
    with coordinator.engine.store.transaction() as db:
        workspace = db.execute("SELECT workspace FROM runs WHERE id='one'").fetchone()[0]
    from pathlib import Path

    coordinator.engine.create("external", definition, Path(workspace), "test", old.revision, 2)
    coordinator.engine.command("external", "resume", "resume-external", 0, 3)
    import time

    deadline = time.monotonic() + 5
    try:
        while (
            time.monotonic() < deadline
            and coordinator.engine.store.get("external").status != "accepted"
        ):
            coordinator.tick()
            time.sleep(0.05)
        assert coordinator.engine.store.get("external").status == "accepted"
        assert coordinator.engine.store.get("one").workflow_digest == old.workflow_digest
    finally:
        coordinator.close()


def test_incompatible_plugin_rejected():
    class Wrong:
        manifest = Manifest("wrong", "1", api_version=999)

    with pytest.raises(ValueError, match="Incompatible"):
        Registry().register(Wrong())


def test_insert_required_prompt_and_simulate():
    from sdd_workflows.templates import main_flow

    original = main_flow()
    changed = insert_step(
        original,
        "implement",
        "done",
        Step("documentation", "agent", "claude", "Write docs", required=True, mutates=True),
        additional_edges=(("repair", "done"), ("reconcile", "done")),
    )
    assert "documentation" in simulate(
        changed, ("done", "done", "done", "done", "passed", "passed")
    )
    assert len(original.steps) + 1 == len(changed.steps)
