import os
import subprocess
import sys
import time

import psutil
import pytest
from sdd_core.codec import canonical
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_runtime.platform import NO_WINDOW, Job
from sdd_storage.store import Store


def runtime(tmp_path, code="print('ok')", timeout=30):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = Engine(Store(tmp_path / "engine.db"))
    workflow = Workflow(
        "check",
        "check",
        (
            Step(
                "check",
                "check",
                "command",
                transitions=(("passed", "finish"), ("failed", "finish")),
                required=True,
                gate=True,
                timeout=timeout,
                config=canonical({"argv": [sys.executable, "-c", code]}),
            ),
            Step("finish", "finish"),
        ),
    )
    definition = engine.store.publish(workflow)
    engine.create("one", definition, workspace, "test", revision(workspace), time.time())
    engine.command("one", "resume", "resume", 0, time.time())
    registry = Registry()
    registry.register(CommandHandler())
    return Coordinator(engine, registry)


def settle(coordinator, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        coordinator.tick()
        run = coordinator.engine.store.get("one")
        if run.status in ("accepted", "blocked"):
            return run
        time.sleep(0.05)
    pytest.fail("Runtime did not settle within deadline")


def test_real_command_acceptance(tmp_path):
    coordinator = runtime(tmp_path)
    try:
        state = settle(coordinator)
        assert state.status == "accepted", state.reason
        assert state.calls == 0
    finally:
        coordinator.close()


def test_failed_check_cannot_accept_even_if_graph_routes_to_finish(tmp_path):
    coordinator = runtime(tmp_path, "raise SystemExit(1)")
    try:
        assert settle(coordinator).status == "blocked"
    finally:
        coordinator.close()


def test_eof_before_gate_executes_nothing(tmp_path):
    marker = tmp_path / "marker"
    (tmp_path / "launch.json").write_text(
        canonical(
            {
                "argv": [sys.executable, "-c", "raise RuntimeError()"],
                "cwd": str(tmp_path),
                "environment": {},
                "nonce": "n",
            }
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-m", "sdd_runtime.host", str(tmp_path)],
        stdin=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    child.stdin.close()
    assert child.wait(timeout=5) == 2
    assert not marker.exists() and not (tmp_path / "exit.json").exists()


def test_host_feeds_a_long_prompt_on_stdin(tmp_path):
    prompt = "Контекст тикета\n" + "x" * 60000
    (tmp_path / "input.txt").write_text(prompt, encoding="utf-8")
    code = "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
    (tmp_path / "launch.json").write_text(
        canonical(
            {
                "argv": [sys.executable, "-c", code],
                "cwd": str(tmp_path),
                "environment": {},
                "input": "input.txt",
                "nonce": "n",
            }
        ),
        encoding="utf-8",
    )
    host = subprocess.run(
        [sys.executable, "-m", "sdd_runtime.host", str(tmp_path)],
        input=b"GO\n",
        timeout=30,
        # As in production, the host leads its own process group on POSIX.
        start_new_session=os.name != "nt",
    )
    assert host.returncode == 0
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8") == prompt
    assert (tmp_path / "exit.json").exists()


def test_command_line_past_the_windows_limit_is_refused_before_launch(tmp_path):
    coordinator = runtime(tmp_path, code="#" + "x" * 40000)
    try:
        coordinator.tick()
        run = coordinator.engine.store.get("one")
        assert "Command line too long" in run.reason
        assert not coordinator.live
    finally:
        coordinator.close()


def test_job_kills_descendants(tmp_path):
    child_id = tmp_path / "child.txt"
    script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    host = tmp_path / "launch.json"
    host.write_text(
        canonical(
            {
                "argv": [sys.executable, "-c", script, str(child_id)],
                "cwd": str(tmp_path),
                "environment": {},
                "nonce": "n",
            }
        )
    )
    job = Job()
    parent = subprocess.Popen(
        [sys.executable, "-m", "sdd_runtime.host", str(tmp_path)],
        stdin=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    try:
        job.assign(parent.pid)
        parent.stdin.write(b"GO\n")
        parent.stdin.close()
        deadline = time.monotonic() + 5
        while not child_id.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_id.exists()
        descendant = psutil.Process(int(child_id.read_text()))
        job.close()
        parent.wait(timeout=5)
        descendant.wait(timeout=5)
    finally:
        job.close()
        parent.wait(timeout=5)


def test_restart_after_coordinator_killed(tmp_path):
    coordinator = runtime(tmp_path, "import time; time.sleep(30)")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sdd_runtime.cli",
            "--database",
            str(coordinator.engine.store.path),
            "run",
        ],
        creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        pid = None
        while time.monotonic() < deadline:
            with coordinator.engine.store.transaction() as db:
                row = db.execute("SELECT pid FROM effects WHERE status='running'").fetchone()
            if row:
                pid = row[0]
                break
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            time.sleep(0.1)
        assert pid
        process.kill()
        process.wait(timeout=5)
        coordinator.restore(time.time())
        state = coordinator.engine.store.get("one")
        assert state.active is None and state.status == "waiting"
        assert state.infrastructure_failures == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
