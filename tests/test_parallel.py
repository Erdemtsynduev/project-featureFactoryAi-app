import sys
import time
from dataclasses import replace

from example_extension import ExampleHandler
from sdd_core.codec import canonical
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_storage.store import Store


def test_pause_and_restart_with_two_parallel_agent_attempts(tmp_path):
    engine = Engine(Store(tmp_path / "engine.db"))
    registry = Registry()
    registry.register(ExampleHandler())
    flow = Workflow(
        "parallel",
        "agent",
        (
            Step("agent", "agent", "example", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    definition = engine.store.publish(flow)
    for name in ("left", "right"):
        root = tmp_path / name
        root.mkdir()
        engine.create(name, definition, root, "task", revision(root), time.time())
        engine.command(name, "resume", "resume-" + name, 0, time.time())
    coordinator = Coordinator(engine, registry)
    coordinator.tick()
    assert len(coordinator.active()) == 2
    left = engine.store.get("left")
    engine.command("left", "pause", "pause-left", left.version, time.time())
    coordinator.close()
    coordinator = Coordinator(engine, registry)
    try:
        coordinator.restore(time.time())
        assert engine.store.get("left").paused
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and engine.store.get("right").status != "accepted":
            coordinator.tick()
            time.sleep(0.05)
        assert engine.store.get("right").status == "accepted"
        assert engine.store.get("left").paused
        assert engine.store.get("left").status != "accepted"
    finally:
        coordinator.close()


def test_failed_task_yields_slot_to_independent_ready_task(tmp_path):
    engine = Engine(Store(tmp_path / "engine.db"))
    flow = Workflow(
        "fair",
        "work",
        (
            Step(
                "work",
                "operation",
                "command",
                transitions=(("done", "finish"), ("failed", "work")),
                config=canonical({"argv": [sys.executable, "-c", "raise SystemExit(1)"]}),
            ),
            Step("finish", "finish"),
        ),
    )
    first = engine.store.publish(flow)
    second = engine.store.publish(
        replace(
            flow,
            steps=(
                replace(
                    flow.steps[0], config=canonical({"argv": [sys.executable, "-c", "print('ok')"]})
                ),
                flow.steps[1],
            ),
        )
    )
    for index, (name, definition) in enumerate((("first", first), ("second", second))):
        root = tmp_path / name
        root.mkdir()
        engine.create(name, definition, root, "", revision(root), index)
        engine.command(name, "resume", "resume-" + name, 0, index)
    registry = Registry()
    registry.register(CommandHandler())
    coordinator = Coordinator(engine, registry)
    try:
        coordinator.tick()
        live = coordinator.supervisor.live[coordinator.active()[0]]
        assert coordinator.packets[live.request.id].run_id == "first"
        live.process.wait(timeout=10)
        coordinator.tick()
        assert engine.store.get("first").generation == 1
        assert engine.store.get("second").active is not None
    finally:
        coordinator.close()
