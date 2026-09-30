import sys
import time

import psutil
import pytest
from sdd_runtime import lineage as lineages
from sdd_runtime.lineage import Lineage
from sdd_runtime.platform import Job
from sdd_runtime.sandbox import Sandbox
from test_interpreters import INSTALL_MANAGER, needs_install_manager
from test_runtime import runtime


class Processes:
    """A process table the test edits, and the processes a lineage ended."""

    def __init__(self, table):
        self.table = dict(table)
        self.ended = []

    def end(self, pid, created):
        self.ended.append(pid)
        if self.table.get(pid, (0, None))[1] == created:
            del self.table[pid]


def lineage_of(processes, root, record=None):
    lineage = Lineage(record, lambda: dict(processes.table), processes.end)
    lineage.add(root)
    return lineage


def test_lineage_follows_descendants_even_after_their_parent_ends():
    processes = Processes({10: (1, 100.0), 11: (10, 101.0), 12: (11, 102.0), 99: (1, 50.0)})
    lineage = lineage_of(processes, 10)
    lineage.observe()
    assert set(lineage.known) == {10, 11, 12}
    # The container killed 10 and 11; 12 left it and lives on as an orphan whose
    # own child appears only now.
    del processes.table[10], processes.table[11]
    processes.table[13] = (12, 103.0)
    lineage.end(timeout=1)
    assert sorted(processes.ended) == [12, 13] and 99 in processes.table


def test_lineage_ignores_a_reused_parent_pid_and_older_processes():
    processes = Processes({10: (1, 100.0), 11: (10, 101.0)})
    lineage = lineage_of(processes, 10)
    lineage.observe()
    del processes.table[10], processes.table[11]
    processes.table[11] = (1, 200.0)  # PID 11 now names an unrelated process
    processes.table[20] = (11, 201.0)  # and that process has a child
    processes.table[21] = (10, 90.0)  # older than the remembered 10: not its child
    lineage.end(timeout=1)
    assert processes.ended == [] and {11, 20, 21} <= set(processes.table)


def test_lineage_reports_a_descendant_it_cannot_end():
    processes = Processes({10: (1, 100.0)})
    lineage = Lineage(None, lambda: dict(processes.table), lambda pid, created: None)
    lineage.add(10)
    with pytest.raises(TimeoutError):
        lineage.end(timeout=0.2)


def test_a_zombie_is_not_a_survivor(monkeypatch):
    # POSIX keeps an ended child in the process table until its parent reads its exit
    # status. It can run nothing, so an end must not wait for it.
    class Listed:
        def __init__(self, pid, parent, created, status):
            self.pid = pid
            self.info = {"ppid": parent, "create_time": created, "status": status}

    listed = [
        Listed(10, 1, 100.0, psutil.STATUS_RUNNING),
        Listed(11, 10, 101.0, psutil.STATUS_ZOMBIE),
        Listed(12, 10, 102.0, psutil.STATUS_SLEEPING),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: listed)
    assert lineages.process_table() == {10: (1, 100.0), 12: (10, 102.0)}
    ended = Lineage(None, lineages.process_table, lambda pid, created: None)
    ended.known = {11: 101.0}
    ended.end(timeout=0.2)  # only a zombie is left: confirmed, no timeout


@needs_install_manager
def test_stop_ends_descendants_that_broke_away_from_the_job(tmp_path):
    # The install manager's `python.exe` runs its interpreter in a job that lets
    # children break away silently: they belong to no job, and a job stop alone
    # left them writing into the workspace after the attempt had settled.
    marker = tmp_path / "late.txt"
    started = tmp_path / "started.txt"
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        '\'import sys,time;time.sleep(3);open(sys.argv[1],"w").write("late")\', sys.argv[1]])\n'
        "open(sys.argv[2], 'w').write('started')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    aggregate = Job()
    sandbox = Sandbox(tmp_path / "attempt", aggregate)
    parent = sandbox.start([str(INSTALL_MANAGER), str(spawner), str(marker), str(started)])
    try:
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            sandbox.observe()
            time.sleep(0.05)
        assert started.exists()
        sandbox.observe()
        # The escape is recorded for the incident report, then ended with the rest.
        assert sandbox.escaped
        sandbox.end()
        time.sleep(4)
        assert not marker.exists()
    finally:
        sandbox.close()
        aggregate.close()
        parent.wait(timeout=5)


def test_stop_ends_a_contained_tree_as_before(tmp_path):
    marker = tmp_path / "late.txt"
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',"
        '\'import sys,time;time.sleep(2);open(sys.argv[1],"w").write("late")\',sys.argv[1]]);'
        "time.sleep(30)"
    )
    aggregate = Job()
    sandbox = Sandbox(tmp_path / "attempt", aggregate)
    parent = sandbox.start([sys.executable, "-c", code, str(marker)])
    try:
        time.sleep(1)
        sandbox.observe()
        assert not sandbox.escaped
        sandbox.end()
        time.sleep(3)
        assert not marker.exists()
    finally:
        sandbox.close()
        aggregate.close()
        parent.wait(timeout=5)


def test_a_restarted_owner_ends_what_the_durable_lineage_remembers(tmp_path):
    processes = Processes({10: (1, 100.0), 11: (10, 101.0)})
    record = tmp_path / "lineage.json"
    lineage_of(processes, 10, record).observe()
    # A new owner process knows only the record.
    Lineage(record, lambda: dict(processes.table), processes.end).end(timeout=1)
    assert sorted(processes.ended) == [10, 11]


@needs_install_manager
def test_timed_out_attempt_leaves_no_writer_behind(tmp_path):
    # The feature_110-T48 incident end to end: a step times out while a process that
    # broke away from its job keeps working. The attempt must end with it, so the
    # workspace does not change after the stop and the run is not blocked as changed
    # "outside attempt".
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        '\'import time; time.sleep(4); open("late.txt", "w").write("late")\'])\n'
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    coordinator = runtime(tmp_path, timeout=2, argv=[str(INSTALL_MANAGER), str(spawner)])
    workspace = tmp_path / "workspace"
    try:
        coordinator.tick()
        deadline = time.monotonic() + 15
        while coordinator.engine.store.get("one").active and time.monotonic() < deadline:
            coordinator.tick()
            time.sleep(0.1)
        state = coordinator.engine.store.get("one")
        assert state.active is None and state.reason == "Attempt timeout"
        time.sleep(4)
        assert not (workspace / "late.txt").exists()
        coordinator.tick(time.time() + 10)
        assert coordinator.engine.store.get("one").reason != "Workspace changed outside attempt"
        assert list(workspace.glob(".sdd-engine/*/*/containment.json"))
    finally:
        coordinator.close()
