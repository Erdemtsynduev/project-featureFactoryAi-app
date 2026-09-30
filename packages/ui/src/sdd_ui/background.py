"""Periodic work beside the queue: each job on its own thread and schedule.

A job's failure is logged and the job runs again at its next time; it never stops the
application or the queue.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from sdd_factory.journal import FlightLog


@dataclass(frozen=True)
class Periodic:
    name: str
    every: float  # seconds between runs
    task: Callable[[], object]
    failure: str  # the log entry a failure writes
    level: str = "error"
    at_start: bool = False  # run once before the first wait


def _run(job: Periodic, quit: threading.Event, log: FlightLog) -> None:
    def attempt() -> None:
        try:
            job.task()
        except Exception as error:  # a background job never stops the application
            log.record(job.failure, job.level, error=f"{type(error).__name__}: {error}")

    if job.at_start:
        attempt()
    while not quit.wait(job.every):
        attempt()


def start(jobs: tuple[Periodic, ...], quit: threading.Event, log: FlightLog) -> None:
    """Start each job on a daemon thread; `quit` ends them all."""
    for job in jobs:
        threading.Thread(target=_run, args=(job, quit, log), name=job.name, daemon=True).start()
