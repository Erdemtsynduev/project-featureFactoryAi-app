"""One execution's processes: started contained, observed while running, ended with proof.

A sandbox pairs an OS container (a Windows job, a POSIX process group) with a
durable lineage of every descendant. The container ends its members at once; the
lineage ends what left it. `end` returns only when both are confirmed empty and
raises otherwise, so an unconfirmed stop is never reported as termination.
Descendants that left the container are recorded in `containment.json` next to the
attempt's other files, for the incident report.
"""

import json
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psutil

from sdd_runtime.files import atomic_write
from sdd_runtime.lineage import Lineage
from sdd_runtime.platform import Containment, Job, start_contained

LINEAGE_FILE = "lineage.json"
ESCAPES_FILE = "containment.json"


class Sandbox:
    def __init__(self, folder: Path, aggregate: Containment) -> None:
        self.folder, self.aggregate = folder, aggregate
        self.container = Job()
        self.lineage = Lineage(folder / LINEAGE_FILE)
        self.escaped: dict[str, str] = {}
        self.root: int | None = None

    def start(self, argv: Sequence[str], **options: Any) -> subprocess.Popen[bytes]:
        """Start the root process; it runs nothing before both containers hold it."""
        process = start_contained(argv, [self.aggregate, self.container], **options)
        self.root = process.pid
        self.lineage.add(process.pid)
        return process

    def observe(self) -> None:
        """Remember new descendants and record any the container does not hold."""
        for pid in self.lineage.observe():
            try:
                if self.container.holds(pid):
                    continue
                name = psutil.Process(pid).name()
            except (OSError, psutil.Error):
                continue  # already gone: nothing escaped
            self.escaped[str(pid)] = name
            atomic_write(self.folder / ESCAPES_FILE, json.dumps(self.escaped, sort_keys=True))

    def end(self, timeout: float = 5) -> None:
        """End the container's members, then every remembered descendant, or raise."""
        deadline = time.monotonic() + timeout
        self.container.stop_and_confirm(timeout)
        self.lineage.end(max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        self.container.close()
        if self.root is not None:
            self.aggregate.forget(self.root)


def end_recorded(folder: Path, timeout: float = 5) -> None:
    """After a restart: end whatever the durable lineage remembers, or raise."""
    Lineage(folder / LINEAGE_FILE).end(timeout)
