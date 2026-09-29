"""Same backend contract through memory and real loopback HTTP transport."""

import json
import threading
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sdd_core.execution import ExecutionHandle, ExecutionObservation, ExecutionRequest
from sdd_core.models import Result, Step, Workflow
from sdd_runtime.engine import Engine
from sdd_runtime.execution import ExecutionDriver
from sdd_runtime.files import revision
from sdd_runtime.http_execution import HttpExecutionBackend
from sdd_storage.store import Conflict, Store


class MemoryBackend:
    id = "test"

    def __init__(self):
        self.requests = {}
        self.observations = {}
        self.cancelled = set()

    def start(self, request):
        if request.id in self.requests and self.requests[request.id] != request:
            raise ValueError("Conflicting request")
        self.requests[request.id] = request
        handle = ExecutionHandle(self.id, request.id, request.generation)
        self.observations.setdefault(request.id, ExecutionObservation(handle, "running"))
        return handle

    def reconcile(self, handle):
        return self.observations.get(handle.id, ExecutionObservation(handle, "unknown"))

    def cancel(self, handle):
        self.cancelled.add(handle.id)


@pytest.fixture(params=["memory", "http"])
def backend(request):
    memory = MemoryBackend()
    if request.param == "memory":
        yield memory, memory
        return

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/executions":
                result = asdict(memory.start(ExecutionRequest(**doc)))
            else:
                parts = self.path.split("/")
                memory.cancel(ExecutionHandle(memory.id, parts[2], int(parts[3])))
                result = {}
            self.respond(result)

        def do_GET(self):
            parts = self.path.split("/")
            self.respond(
                asdict(memory.reconcile(ExecutionHandle(memory.id, parts[2], int(parts[3]))))
            )

        def respond(self, result):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpExecutionBackend("test", f"http://127.0.0.1:{server.server_port}"), memory
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def setup_driver(tmp_path, backend):
    root = tmp_path / "project"
    root.mkdir()
    engine = Engine(Store(tmp_path / "engine.db"))
    definition = engine.store.publish(
        Workflow(
            "remote",
            "work",
            (
                Step("work", "operation", "external", transitions=(("done", "finish"),)),
                Step("finish", "finish"),
            ),
        )
    )
    engine.create("r", definition, root, "", revision(root), 0)
    engine.command("r", "resume", "resume", 0, 0)
    engine.dispatch("r", 1, "attempt")
    return ExecutionDriver(engine, backend)


def test_repeated_submit_and_restart_collect_without_reexecution(tmp_path, backend):
    adapter, memory = backend
    driver = setup_driver(tmp_path, adapter)
    handle = driver.submit("r", "payload")
    assert driver.submit("r", "payload") == handle
    with pytest.raises(Conflict):
        driver.submit("r", "different")
    state = driver.engine.store.get("r")
    memory.observations[handle.id] = ExecutionObservation(
        handle, "completed", Result(handle.id, 1, "done", "", state.revision), completed_at=2
    )
    restarted = ExecutionDriver(Engine(Store(driver.engine.store.path)), adapter)
    restarted.poll("r", 1000)  # Receipt predates timeout; delayed delivery is safe.
    restarted.poll("r", 1001)
    assert restarted.engine.dispatch("r", 1002, "finish").status == "accepted"
    assert len(memory.requests) == 1


def test_cancel_is_not_termination_and_unknown_retains_owner(tmp_path, backend):
    adapter, memory = backend
    driver = setup_driver(tmp_path, adapter)
    handle = driver.submit("r", "payload")
    driver.poll("r", 999)
    assert handle.id in memory.cancelled
    assert driver.engine.store.get("r").active is not None
    memory.observations[handle.id] = ExecutionObservation(handle, "unknown")
    driver.poll("r", 1000)
    state = driver.engine.store.get("r")
    assert state.active is not None and state.status == "blocked"
    memory.observations[handle.id] = ExecutionObservation(handle, "terminated")
    driver.poll("r", 1001)
    assert driver.engine.store.get("r").active is None


def test_old_generation_cannot_complete_current_attempt(tmp_path, backend):
    adapter, memory = backend
    driver = setup_driver(tmp_path, adapter)
    handle = driver.submit("r", "payload")
    memory.observations[handle.id] = ExecutionObservation(
        replace(handle, generation=0), "terminated"
    )
    with pytest.raises(ValueError, match="Stale"):
        driver.poll("r", 2)
    assert driver.engine.store.get("r").active is not None


def test_coordinator_leaves_another_backends_attempt_alone(tmp_path, backend):
    from sdd_core.sdk import Registry
    from sdd_runtime.coordinator import Coordinator

    adapter, _ = backend
    driver = setup_driver(tmp_path, adapter)
    driver.submit("r", "payload")
    coordinator = Coordinator(driver.engine, Registry())
    try:
        before = driver.engine.store.get("r")
        coordinator.restore(20)
        coordinator.collect(20)
        assert driver.engine.store.get("r") == before
        with pytest.raises(Conflict):
            coordinator.driver.submit("r", "payload")
    finally:
        coordinator.close()
