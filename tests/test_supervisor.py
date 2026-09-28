import json
import subprocess
import sys
import time

import psutil
import pytest
from sdd_runtime.platform import NO_WINDOW
from test_runtime import runtime


@pytest.mark.parametrize("iteration", range(5))
def test_supervisor_death_terminates_coordinator_and_worker(tmp_path, iteration):
    coordinator = runtime(tmp_path, "import time; time.sleep(60)")
    database = coordinator.engine.store.path
    process = subprocess.Popen(
        [sys.executable, "-m", "sdd_runtime.cli", "--database", str(database), "supervise"],
        creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        worker = None
        while time.monotonic() < deadline:
            with coordinator.engine.store.transaction() as db:
                row = db.execute("SELECT pid FROM effects WHERE status='running'").fetchone()
            if row:
                worker = psutil.Process(row[0])
                break
            assert process.poll() is None, process.stderr.read().decode()
            time.sleep(0.05)
        assert worker is not None
        descriptor = json.loads(database.with_suffix(".supervisor.json").read_text())
        child = psutil.Process(descriptor["coordinator_pid"])
        process.kill()
        process.wait(timeout=5)
        worker.wait(timeout=5)
        child.wait(timeout=5)
        coordinator.restore(time.time())
        state = coordinator.engine.store.get("one")
        assert state.status == "waiting"
        assert coordinator.engine.store.replay("one") == state
        with coordinator.engine.store.transaction() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        coordinator.close()
