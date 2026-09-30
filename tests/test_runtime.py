import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import psutil
import pytest
from sdd_core.codec import canonical
from sdd_core.execution import ExecutionRequest
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_providers.handlers import CommandHandler
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_runtime.platform import NO_WINDOW, Job, start_contained
from sdd_runtime.supervisor import Plan
from sdd_storage.store import Store


def runtime(tmp_path, code="print('ok')", timeout=30, argv=None):
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
                config=canonical({"argv": argv or [sys.executable, "-c", code]}),
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


def host_pid(tmp_path):
    """The PID a running attempt's host recorded, once it has (see `runtime`: the
    engine's work folder lies beside its database, not in the workspace)."""
    for identity in tmp_path.glob("engine.work/engine/*/*/identity.json"):
        return json.loads(identity.read_text(encoding="utf-8"))["pid"]
    return None


def finished_execution(engine, run_id, packet, completed_at, exit_code=0):
    """An execution whose host ended while no coordinator watched it: the durable
    request, the identity of a process that is gone and the host's exit receipt."""
    workspace = packet.workspace
    plan = Plan(packet.directory, workspace, (sys.executable,), workspace)
    attempt = packet.attempt
    request = ExecutionRequest(
        attempt.id, attempt.generation, "handler", plan.payload(), attempt.deadline
    )
    with engine.store.unit() as unit:
        unit.bind_execution(run_id, attempt.id, "local", canonical(asdict(request)))
    ended = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(0.5)"])
    created = psutil.Process(ended.pid).create_time()
    ended.wait(timeout=10)
    folder = Path(packet.directory)
    (folder / "identity.json").write_text(
        canonical({"pid": ended.pid, "created": created, "nonce": "nonce"}), encoding="utf-8"
    )
    (folder / "exit.json").write_text(
        canonical({"exit_code": exit_code, "completed_at": completed_at, "nonce": "nonce"}),
        encoding="utf-8",
    )


def hosted(coordinator):
    """The executions whose processes the coordinator's supervisor owns now."""
    return [coordinator.supervisor.live[i] for i in coordinator.active()]


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
        assert state.spend.calls == 0
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
        assert not coordinator.active()
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


class SlowJob:
    """A job whose assignment lags, as a loaded coordinator's does."""

    def __init__(self):
        self.job = Job()

    def assign(self, pid):
        time.sleep(0.5)
        self.job.assign(pid)

    def __getattr__(self, name):
        return getattr(self.job, name)


def test_late_assignment_still_contains_launcher_children(tmp_path):
    # A venv `python.exe` launcher starts the real interpreter at once; assigned too
    # late, that child and all it starts stayed outside the job and outlived a
    # confirmed stop, writing into the workspace after the attempt had settled.
    marker = tmp_path / "late.txt"
    child_id = tmp_path / "child.txt"
    script = (
        "import subprocess,sys,time; from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c',"
        '\'import sys,time;time.sleep(3);open(sys.argv[1],"w").write("late")\',sys.argv[2]]);'
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    (tmp_path / "launch.json").write_text(
        canonical(
            {
                "argv": [sys.executable, "-c", script, str(child_id), str(marker)],
                "cwd": str(tmp_path),
                "environment": {},
                "nonce": "n",
            }
        )
    )
    job = SlowJob()
    parent = start_contained(
        [sys.executable, "-m", "sdd_runtime.host", str(tmp_path)], [job], stdin=subprocess.PIPE
    )
    try:
        parent.stdin.write(b"GO\n")
        parent.stdin.close()
        deadline = time.monotonic() + 10
        while not child_id.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_id.exists()
        job.stop_and_confirm()
        time.sleep(4)
        assert not marker.exists()
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
            pid = host_pid(tmp_path)
            if pid:
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


def test_a_group_of_only_zombies_counts_as_stopped(monkeypatch):
    # macOS answers EPERM when every member of the group is a zombie; Linux succeeds.
    from sdd_runtime import platform
    from sdd_runtime.platform import ProcessGroups

    def refuse(group: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(platform, "kill_group", refuse)
    job = ProcessGroups()
    job.groups.add(4741)
    monkeypatch.setattr(platform, "group_alive", lambda group: False)
    job.stop_and_confirm()
    job.close()
    assert job.groups == set()
    # A live member that may not be signalled is never reported as stopped.
    job.groups.add(4741)
    monkeypatch.setattr(platform, "group_alive", lambda group: True)
    with pytest.raises(PermissionError):
        job.terminate()
