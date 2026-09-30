"""Background jobs run on their own schedule and survive their own failures."""

import threading

from sdd_factory.journal import FlightLog
from sdd_ui.background import Periodic, start


def test_a_failing_job_is_logged_and_runs_again(tmp_path):
    log = FlightLog(tmp_path / "flight.jsonl")
    quit = threading.Event()
    calls: list[int] = []
    done = threading.Event()

    def task() -> None:
        calls.append(1)
        if len(calls) >= 3:
            done.set()
        raise RuntimeError("probe failed")

    start((Periodic("probe", 0.01, task, "probe_failed", "warning", at_start=True),), quit, log)
    assert done.wait(5), "the job keeps running after failures"
    quit.set()
    entries = log.read(limit=10)
    assert entries and entries[0]["kind"] == "probe_failed" and entries[0]["level"] == "warning"
