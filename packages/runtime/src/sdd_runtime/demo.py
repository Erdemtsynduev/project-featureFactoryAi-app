"""Bounded, isolated command-only demonstration of persistence and acceptance."""

import sys
import time
import uuid
from pathlib import Path

from sdd_core.codec import canonical
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry

from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_runtime.lock import Lease


def demonstrate(engine: Engine, registry: Registry, directory: Path) -> str:
    workspace = directory / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    flow = Workflow(
        "demo",
        "check",
        (
            Step(
                "check",
                "check",
                "command",
                transitions=(("passed", "finish"),),
                required=True,
                gate=True,
                timeout=15,
                config=canonical(
                    {"argv": [sys.executable, "-c", "print('Feature Factory AI: verified')"]}
                ),
            ),
            Step("finish", "finish"),
        ),
    )
    identifier = "demo-" + uuid.uuid4().hex[:12]
    with Lease(directory / "demo.lock"):
        definition = engine.store.publish(flow)
        engine.create(
            identifier,
            definition,
            workspace,
            "Command-only demonstration",
            revision(workspace),
            time.time(),
        )
        engine.command(identifier, "resume", identifier, 0, time.time())
        coordinator = Coordinator(engine, registry, directory / "health.json")
        try:
            coordinator.restore(time.time())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                coordinator.tick()
                run = engine.store.get(identifier)
                if run.status == "accepted":
                    engine.store.replay(identifier)
                    return identifier
                if run.status == "blocked":
                    raise RuntimeError(run.reason)
                time.sleep(0.05)
            raise TimeoutError("Demo did not settle in 20 seconds")
        finally:
            coordinator.close()
