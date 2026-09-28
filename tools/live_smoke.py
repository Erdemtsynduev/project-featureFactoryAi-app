"""One bounded read-only model call per explicitly supplied provider executable."""

import argparse
import subprocess
import time
from pathlib import Path

from sdd_core.codec import canonical
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_providers.handlers import CliHandler
from sdd_providers.structured_cli import Invocation, StructuredCliHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write, revision
from sdd_storage.store import Store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=["codex", "claude", "cursor", "opencode"])
    parser.add_argument("executable")
    parser.add_argument("--argument", action="append", default=[])
    parser.add_argument("--directory", type=Path, default=Path("reports/live"))
    args = parser.parse_args()
    folder = (args.directory / args.provider / str(time.time_ns())).resolve()
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True, timeout=10)
    engine = Engine(Store(folder / "engine.db"))
    workflow = Workflow(
        "smoke",
        "smoke",
        (
            Step(
                "smoke",
                "agent",
                args.provider,
                'Do not use any tools. Return outcome "done", reason "smoke", standards true, specification true. This is only a protocol test.',
                (("done", "finish"),),
                required=True,
                timeout=120,
                max_visits=1,
                mutates=args.provider == "opencode",
            ),
            Step("finish", "finish"),
        ),
        max_calls=1,
    )
    definition = engine.store.publish(workflow)
    registry = Registry()
    registry.register(
        StructuredCliHandler(args.provider, Invocation(args.executable, tuple(args.argument)))
        if args.provider in ("cursor", "opencode")
        else CliHandler(args.provider, args.executable)
    )
    engine.create(
        "smoke",
        definition,
        workspace,
        "Read-only protocol qualification; no implementation work.",
        revision(workspace),
        time.time(),
    )
    engine.command("smoke", "resume", "resume", 0, time.time())
    coordinator = Coordinator(engine, registry)
    try:
        deadline = time.monotonic() + 140
        while time.monotonic() < deadline:
            coordinator.tick()
            state = engine.store.get("smoke")
            if state.status in ("accepted", "blocked"):
                report = {
                    "provider": args.provider,
                    "status": state.status,
                    "calls": state.calls,
                    "tokens": state.tokens,
                    "usage_unknown": state.usage_unknown,
                    "reason": state.reason,
                }
                atomic_write(folder / "report.json", canonical(report))
                print(canonical(report))
                return 0 if state.status == "accepted" else 1
            time.sleep(0.2)
        raise TimeoutError("Live qualification timed out")
    finally:
        coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
