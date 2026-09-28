import time

import pytest
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
from sdd_runtime.engine import Engine
from sdd_storage.store import Store
from test_runtime import runtime


def test_stop_kills_only_owned_attempt_and_preserves_pause(tmp_path):
    coordinator = runtime(tmp_path, "import time;time.sleep(20)")
    try:
        coordinator.tick()
        live = next(iter(coordinator.live.values()))
        state = coordinator.engine.store.get("one")
        coordinator.engine.command("one", "stop", "stop", state.version, time.time())
        coordinator.tick()
        state = coordinator.engine.store.get("one")
        assert state.paused and state.active is None
        assert live.process.poll() is not None
        coordinator.tick(time.time() + 1000)
        assert coordinator.engine.store.get("one") == state
    finally:
        coordinator.close()


def test_human_wait_does_not_expire_or_consume_calls(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = Engine(Store(tmp_path / "engine.db"))
    workflow = Workflow(
        "human",
        "question",
        (
            Step("question", "human", transitions=(("answered", "finish"),), required=True),
            Step("finish", "finish"),
        ),
    )
    definition = engine.store.publish(workflow)
    engine.create("human", definition, workspace, "question", "rev", 0)
    engine.command("human", "resume", "r", 0, 1)
    engine.dispatch("human", 2, "a")
    version = engine.store.get("human").version
    engine.answer("human", "answered", "answer", {}, version, 100000)
    state = engine.store.get("human")
    assert state.calls == 0 and state.step == "finish"
    with pytest.raises(Conflict):
        engine.answer("human", "answered", "again", {}, version, 100001)


def test_subscription_wait_does_not_reset_call_budget(tmp_path):
    coordinator = runtime(tmp_path)
    engine = coordinator.engine
    engine.dispatch("one", 2, "a")
    state = engine.complete(
        "one",
        Result(
            "a", 1, "waiting", "subscription reset", engine.store.get("one").revision, resume_at=200
        ),
        3,
    )
    assert state.status == "waiting" and dict(state.visits)["check"] == 0
    with pytest.raises(Conflict, match="not dispatchable"):
        engine.dispatch("one", 100, "b")
    coordinator.close()


def test_project_adapter_injected_without_runtime_changes(tmp_path):
    from example_extension import ExampleProject

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "project.txt").write_text("external project")
    project = ExampleProject()
    engine = Engine(Store(tmp_path / "db"), project)
    assert engine.project.revision(str(workspace)) == project.revision(str(workspace))
