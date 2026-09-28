import json
import os
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sdd_core import machine
from sdd_core.codec import canonical, workflow_json, workflow_load
from sdd_core.models import Result, Run, Step, Workflow
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import atomic_write
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_storage.memory import MemoryStore
from sdd_storage.store import Store
from sdd_ui.server import create_server
from sdd_ui.service import WorkspaceService
from sdd_workflows.templates import feature, interview, main_flow


def test_planning_budget_is_distinct_from_implementation():
    flow = Workflow(
        "budget",
        "plan",
        (
            Step(
                "plan",
                "agent",
                "fake",
                config='{"purpose":"planning"}',
                transitions=(("done", "plan"),),
            ),
        ),
        max_calls=20,
        max_planning_calls=1,
    )
    state = Run("one", "digest", "plan", "rev", paused=False)
    state = machine.dispatch(state, flow, 1, "a").state
    state = machine.complete(state, flow, Result("a", 1, "done", "ok", "rev"), 2).state
    state = machine.dispatch(state, flow, 3, "b").state
    assert state.status == "blocked" and state.calls == state.planning_calls == 1
    assert state.active is None
    assert workflow_load(workflow_json(flow)) == flow
    assert feature().max_planning_calls == 0 and feature().entry == "approve"
    assert main_flow().max_planning_calls == 4
    assert interview().max_planning_calls == 4
    assert json.loads(interview().step("ask").config)["purpose"] == "planning"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_queue_budget_is_shared_and_survives_new_engine(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    flow = Workflow(
        "calls",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace(), max_queue_calls=1)
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        engine.create(name, definition, root, "", "rev", 0)
        engine.command(name, "resume", name, 0, 1)
    assert engine.dispatch("one", 2, "first").active
    before = store.get("one")
    with pytest.raises(ValueError, match="not dispatchable"):
        engine.dispatch("one", 2, "duplicate")
    assert store.get("one") == before
    restarted = ApplicationEngine(store, GitProject(), LocalWorkspace(), max_queue_calls=1)
    blocked = restarted.dispatch("two", 3, "second")
    assert blocked.status == "blocked" and blocked.calls == 0


def test_atomic_replace_retries_only_temporary_permission_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("old")
    original = os.replace
    calls = []

    def flaky(source, target):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("sharing conflict")
        original(source, target)

    monkeypatch.setattr("sdd_runtime.files.os.replace", flaky)
    atomic_write(path, "new")
    assert path.read_text() == "new" and len(calls) == 3
    monkeypatch.setattr(
        "sdd_runtime.files.os.replace",
        lambda *args: (_ for _ in ()).throw(PermissionError("permanent")),
    )
    with pytest.raises(PermissionError):
        atomic_write(path, "lost")
    assert path.read_text() == "new"


def test_host_overrides_legacy_python_encoding(tmp_path):
    (tmp_path / "launch.json").write_text(
        canonical(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import os; print('Привет 世界'); print(os.environ['TEMP'])",
                ],
                "cwd": str(tmp_path),
                "environment": {"PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"},
                "nonce": "test",
            }
        ),
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, "-m", "sdd_runtime.host", str(tmp_path)], input=b"GO\n", timeout=10
    )
    assert process.returncode == 0
    output = (tmp_path / "stdout.log").read_text(encoding="utf-8")
    assert "Привет 世界" in output and str(tmp_path / "tmp") in output


def test_ui_http_security_and_complete_demo_lifecycle(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    server = create_server(service, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    worker = threading.Thread(target=service.work, daemon=True)
    thread.start()
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/state", timeout=5) as response:
            state = json.load(response)
        assert state["settings"]["running"] is False and state["totals"]["calls"] == 0
        status = 200
        for _ in range(5):  # the first worker tick may change the state in between
            with urlopen(base + "/api/state", timeout=5) as response:
                tag = response.headers["ETag"]
            try:
                urlopen(Request(base + "/api/state", headers={"If-None-Match": tag}), timeout=5)
            except HTTPError as unchanged:
                status = unchanged.code
                break
        assert status == 304, "an unchanged board is not re-sent"

        def post(action, body, authorized=True):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers.update({"Origin": base, "X-FFAI-Token": state["token"]})
            with urlopen(
                Request(base + "/api/" + action, canonical(body).encode(), headers), timeout=10
            ) as response:
                return json.load(response)

        with pytest.raises(HTTPError) as error:
            post("queue", {"running": True}, False)
        assert error.value.code == 403
        flow = post("template", {"name": "demo"})
        definition = post("publish", {"workflow": flow})["digest"]
        workspace = tmp_path / "project"
        workspace.mkdir()
        run = post(
            "create",
            {
                "id": "demo",
                "definition": definition,
                "workspace": str(workspace),
                "context": "Test",
            },
        )
        assert run["paused"] and run["calls"] == 0
        post("resume", {"id": "demo", "version": 0, "request_id": "resume"})
        with pytest.raises(HTTPError) as stale:
            post("pause", {"id": "demo", "version": 0})
        assert stale.value.code == 409
        post("queue", {"running": True})
        deadline = time.monotonic() + 10
        while service.engine.store.get("demo").status != "accepted" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert service.engine.store.get("demo").status == "accepted"
        post("queue", {"running": False})
        post("budget", {"max_calls": 12, "max_planning_calls": 2})
        assert service.engine.max_queue_calls == 12
    finally:
        service.close()
        worker.join(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    reopened = WorkspaceService(tmp_path / "ui.db")
    try:
        assert reopened.settings["running"] is False
        assert reopened.engine.max_queue_calls == 12
    finally:
        reopened.coordinator.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job race")
def test_assign_completed_process_and_existing_member(monkeypatch):
    from sdd_runtime.platform import Job

    completed = subprocess.Popen([sys.executable, "-c", "pass"])
    completed.wait(timeout=5)
    job = Job()
    try:
        job.assign(completed.pid)
        live = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(15)"])
        try:
            job.assign(live.pid)
            job.assign(live.pid)
            assert job.contains(live.pid)
            assert not job.contains(os.getpid())
            # Query the real kernel job after simulating denied process-handle access.
            monkeypatch.setattr(job.kernel, "OpenProcess", lambda *args: 0)
            job.assign(live.pid)
            with pytest.raises(OSError):
                job.assign(os.getpid())
        finally:
            job.close()
            live.wait(timeout=5)
    finally:
        job.close()


def test_human_answer_is_versioned_and_not_automatic(tmp_path):
    from sdd_core.ports import Conflict

    service = WorkspaceService(tmp_path / "ui.db")
    try:
        flow = Workflow(
            "human",
            "approve",
            (
                Step("approve", "human", transitions=(("approved", "finish"),), required=True),
                Step("finish", "finish"),
            ),
        )
        workspace = tmp_path / "project"
        workspace.mkdir()
        definition = service.engine.store.publish(flow)
        service.mutate(
            "create", {"id": "one", "definition": definition, "workspace": str(workspace)}
        )
        service.mutate("resume", {"id": "one", "version": 0})
        service.coordinator.tick()
        run = service.engine.store.get("one")
        assert run.active and run.calls == 0
        with pytest.raises(Conflict):
            service.mutate(
                "answer", {"id": "one", "version": 0, "outcome": "approved", "answer": "Yes"}
            )
        assert service.engine.store.get("one") == run
        service.mutate(
            "answer",
            {
                "id": "one",
                "version": run.version,
                "outcome": "approved",
                "answer": "Scope approved",
            },
        )
        service.coordinator.tick()
        assert service.engine.store.get("one").status == "accepted"
    finally:
        service.coordinator.close()


def test_stop_is_collected_while_queue_is_paused(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        flow = Workflow(
            "stop",
            "check",
            (
                Step(
                    "check",
                    "check",
                    "command",
                    transitions=(("passed", "finish"),),
                    config=canonical(
                        {"argv": [sys.executable, "-c", "import time;time.sleep(60)"]}
                    ),
                ),
                Step("finish", "finish"),
            ),
        )
        workspace = tmp_path / "project"
        workspace.mkdir()
        definition = service.engine.store.publish(flow)
        service.mutate(
            "create", {"id": "one", "definition": definition, "workspace": str(workspace)}
        )
        service.mutate("resume", {"id": "one", "version": 0})
        service.coordinator.tick()
        run = service.engine.store.get("one")
        assert run.active and service.coordinator.live
        service.mutate("stop", {"id": "one", "version": run.version})
        assert service.settings["running"] is False
        for identifier in tuple(service.coordinator.live):
            service.coordinator.collect(identifier, time.time())
        stopped = service.engine.store.get("one")
        assert stopped.paused and stopped.active is None
        assert not service.coordinator.live
    finally:
        service.coordinator.close()
