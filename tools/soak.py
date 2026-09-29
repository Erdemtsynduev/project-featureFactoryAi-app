"""Real wall-clock soak. Durable progress never labels an unfinished run as passed."""

import argparse
import hashlib
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from sdd_core.codec import canonical
from sdd_core.models import Result, Step, Workflow
from sdd_core.sdk import Manifest, Packet, Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write, revision
from sdd_runtime.lock import Lease
from sdd_storage.store import Store


class SoakHandler(CommandHandler):
    # Agent slots exercise parallel scheduling, but this provider never calls a model.
    manifest = Manifest("command", "0.1.0", capabilities=("agent", "check", "process"))

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        return replace(
            super().collect(packet, exit_code, revision), standards=True, specification=True
        )


def source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    checksum = hashlib.sha256()
    paths = list(root.glob("packages/*/src/**/*.py"))
    paths += [path for path in root.glob("packages/*/src/**/static/*") if path.is_file()]
    paths += list((root / "tests" / "fixtures").glob("*.jsonl"))
    paths += list((root / "examples" / "extension").glob("*.py"))
    paths += list((root / "tests").glob("*.py")) + list((root / "tools").glob("*.py"))
    paths += list(root.glob("packages/*/pyproject.toml"))
    paths += [
        root / "pyproject.toml",
        root / "requirements-dev.lock",
        root / "uv.lock",
        root / "install.py",
        root / "ffai.py",
        root / "ffai",
        root / "ffai.cmd",
    ]
    for path in sorted(paths):
        checksum.update(path.relative_to(root).as_posix().encode())
        checksum.update(path.read_bytes())
    return checksum.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=86400)
    parser.add_argument("--directory", type=Path, default=Path("reports/soak"))
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--parallel", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    root = args.directory.resolve()
    workspaces = [root / f"workspace-{index}" for index in range(args.parallel)]
    for workspace in workspaces:
        workspace.mkdir(parents=True, exist_ok=True)
    started = time.time()
    source = source_fingerprint()
    deadline = time.monotonic() + args.seconds
    completed = 0
    recovered = 0
    registry = Registry()
    registry.register(SoakHandler())
    engine = Engine(Store(root / "engine.db"))
    workflow = Workflow(
        "soak",
        "check",
        (
            Step(
                "check",
                "agent",
                "command",
                transitions=(("passed", "finish"), ("failed", "finish")),
                required=True,
                gate=True,
                config=canonical(
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time; time.sleep(.2); print('verified')",
                        ]
                    }
                ),
            ),
            Step("finish", "finish"),
        ),
    )
    definition = engine.store.publish(workflow)
    coordinator = Coordinator(engine, registry)
    report = {
        "status": "running",
        "started": started,
        "requested_seconds": args.seconds,
        "completed": 0,
        "recovered": 0,
        "model_calls": 0,
        "source_sha256": source,
        "parallel": args.parallel,
        "max_observed_parallel": 0,
    }
    try:
        with Lease(root / "soak.lock"):
            coordinator.restore(time.time())
            while time.monotonic() < deadline:
                if source_fingerprint() != source:
                    raise RuntimeError(
                        "Engine source changed during soak; start a new qualified run"
                    )
                identifiers = []
                for workspace in workspaces:
                    identifier = "soak-" + str(time.time_ns())
                    identifiers.append(identifier)
                    engine.create(
                        identifier,
                        definition,
                        workspace,
                        "deterministic soak",
                        revision(workspace),
                        time.time(),
                    )
                    engine.command(identifier, "resume", identifier + "-resume", 0, time.time())
                local_deadline = time.monotonic() + 30
                injected = False
                settled = set()
                while time.monotonic() < local_deadline:
                    coordinator.tick()
                    report["max_observed_parallel"] = max(
                        report["max_observed_parallel"], len(coordinator.active())
                    )
                    if completed % 7 == 6 and not injected and coordinator.active():
                        live = coordinator.supervisor.live[coordinator.active()[0]]
                        live.sandbox.container.terminate()
                        live.process.wait(timeout=5)
                        injected = True
                        recovered += 1
                    for identifier in identifiers:
                        state = engine.store.get(identifier)
                        if state.status == "accepted" and identifier not in settled:
                            settled.add(identifier)
                            completed += 1
                            engine.store.replay(identifier)
                        if state.status == "blocked":
                            raise RuntimeError(canonical(asdict(state)))
                    if len(settled) == len(identifiers):
                        break
                    time.sleep(0.05)
                else:
                    raise TimeoutError("Soak task failed to settle")
                report.update(
                    completed=completed,
                    recovered=recovered,
                    heartbeat=time.time(),
                    elapsed=time.time() - started,
                )
                atomic_write(root / "progress.json", canonical(report))
                time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
            report.update(status="passed", finished=time.time(), elapsed=time.time() - started)
            atomic_write(root / "progress.json", canonical(report))
    except BaseException as error:
        report.update(status="failed", error=str(error), finished=time.time())
        atomic_write(root / "progress.json", canonical(report))
        raise
    finally:
        coordinator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
