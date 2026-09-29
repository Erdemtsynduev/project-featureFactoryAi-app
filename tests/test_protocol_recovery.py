from pathlib import Path

from sdd_core.models import Step, Workflow
from sdd_core.sdk import Manifest, Registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_storage.store import Store
from test_runtime import finished_execution


class BrokenProtocol:
    manifest = Manifest("broken", "1", capabilities=("agent",))

    def prepare(self, packet):
        raise AssertionError("A new model call is forbidden")

    def collect(self, packet, exit_code, revision):
        raise ValueError("truncated structured answer")


def test_recovery_preserves_protocol_diagnosis_without_second_call(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = Engine(Store(tmp_path / "state.db"))
    flow = Workflow(
        "protocol",
        "work",
        (
            Step("work", "agent", "broken", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
        max_calls=1,
    )
    engine.create("one", engine.store.publish(flow), workspace, "test", revision(workspace), 0)
    engine.command("one", "resume", "start", 0, 1)
    engine.dispatch("one", 2, "attempt")
    registry = Registry()
    registry.register(BrokenProtocol())
    coordinator = Coordinator(engine, registry)
    try:
        packet = coordinator.packet("one")
        finished_execution(engine, "one", packet, completed_at=3)
        coordinator.restore(4)
        state = engine.store.get("one")
        assert state.status == "blocked" and state.spend.calls == 1
        assert state.reason == "Provider protocol: truncated structured answer"
        assert state.infrastructure_failures == 0
        assert Path(packet.directory, "receipt.json").exists()
        coordinator.restore(5)
        coordinator.tick()
        assert engine.store.get("one") == state
    finally:
        coordinator.close()
