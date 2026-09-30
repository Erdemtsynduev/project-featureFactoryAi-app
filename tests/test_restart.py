"""Restart from the UI: the server stops, a fresh process takes the port and the lease."""

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from sdd_storage.store import Store
from sdd_ui import server as server_module
from sdd_ui.server import launch, relaunch_command

REAL_POPEN = subprocess.Popen


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def info(port: int, **query: str) -> dict[str, object]:
    suffix = "?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
    with urlopen(f"http://127.0.0.1:{port}/api/info{suffix}", timeout=2) as response:
        return dict(json.load(response))


def wait_for(
    port: int,
    seconds: float = 30,
    process: subprocess.Popen[bytes] | None = None,
    log: Path | None = None,
) -> dict[str, object]:
    """The server's info once it answers; a server process that exits fails at once."""
    deadline = time.time() + seconds
    while True:
        try:
            return info(port)
        except OSError as error:
            output = log.read_text(encoding="utf-8", errors="replace") if log else ""
            if process is not None and process.poll() is not None:
                raise AssertionError(
                    f"Server exited with {process.returncode}:\n{output}"
                ) from error
            if time.time() > deadline:
                raise AssertionError(f"Server did not answer in {seconds}s:\n{output}") from error
            time.sleep(0.2)


def state(port: int) -> dict[str, Any]:
    with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10) as response:
        return dict(json.load(response))


def post(port: int, action: str, body: bytes = b"{}") -> dict[str, object]:
    base = f"http://127.0.0.1:{port}"
    with urlopen(base + "/api/state", timeout=10) as response:
        token = json.load(response)["token"]
    request = Request(
        f"{base}/api/{action}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-FFAI-Token": token, "Origin": base},
    )
    with urlopen(request, timeout=10) as response:
        return dict(json.load(response))


def test_restart_relaunches_the_same_server_and_keeps_the_queue_state(tmp_path, monkeypatch):
    database = tmp_path / "ui.db"
    port = free_port()
    spawned: list[list[str]] = []
    monkeypatch.setattr(server_module.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        server_module.subprocess, "Popen", lambda command, **kwargs: spawned.append(command)
    )
    thread = threading.Thread(target=launch, args=(database, None, port, False), daemon=True)
    thread.start()
    first = wait_for(port)
    assert info(port, stale="1")["stale"] is False  # the code did not change while it ran
    assert state(port)["settings"]["running"] is False  # opening never starts work
    post(port, "queue", b'{"running": true}')
    assert post(port, "restart") == {"restart": True, "stopping": True}
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert spawned == [relaunch_command(database.resolve(), None, port)]

    # The relaunch command really starts the server again, on the same port.
    # A fresh interpreter imports the whole application: slow CI runners need time.
    log = tmp_path / "relaunch.log"
    with log.open("wb") as output:
        process = REAL_POPEN(
            spawned[0], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT
        )
    try:
        second = wait_for(port, 90, process, log)
        assert second["database"] == str(database.resolve())
        assert second["started"] != first["started"]
        assert state(port)["settings"]["running"] is True  # a restart is not a pause
        assert post(port, "shutdown")["restart"] is False
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    stored = Store(database).catalog().preference("queue-settings")
    assert stored is not None and json.loads(stored)["running"] is False


def test_info_reports_code_newer_than_the_running_server(tmp_path, monkeypatch):
    from sdd_ui.server import create_server
    from sdd_ui.service import WorkspaceService

    stamps = iter([1.0, 1.0, 2.0])
    monkeypatch.setattr(server_module, "code_stamp", lambda: next(stamps))
    service = WorkspaceService(tmp_path / "ui.db")
    http = create_server(service, 0)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        port = http.server_port
        assert info(port, stale="1")["stale"] is False
        assert info(port, stale="1")["stale"] is True  # someone updated the code
        assert info(port)["stale"] is False  # only computed when asked
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)
        service.coordinator.close()
