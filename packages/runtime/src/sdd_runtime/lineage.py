"""Descendants remembered by parent links, so a stop also ends what left its container.

A job or process group is the first line of containment, and not a complete one.
On Windows a launcher from a packaged app (the Python install manager's
`python.exe` alias) runs its interpreter in the package's own job, which lets
children break away silently; those children belong to no job at all. On POSIX a
descendant may call `setsid`. Parent links still name every such process while
its parent's identity resolves, so the owner observes them while the work runs.

The record is durable: after a coordinator restart the remembered processes can
still be ended, identified by PID and creation time, never by PID alone.
"""

import json
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from sdd_runtime.files import atomic_write

# Process identity: a PID is only the same process while its creation time matches.
SAME_PROCESS = 0.01

# pid -> (parent pid, creation time)
Table = dict[int, tuple[int, float]]


def process_table() -> Table:
    table: Table = {}
    for process in psutil.process_iter(["ppid", "create_time"]):
        parent, created = process.info["ppid"], process.info["create_time"]
        if parent is not None and created is not None:
            table[process.pid] = (int(parent), float(created))
    return table


def kill(pid: int, created: float) -> None:
    """End `pid` only while it is still the remembered process."""
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) < SAME_PROCESS:
            process.kill()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        pass


class Lineage:
    """Every process descended from the roots, remembered while links resolve."""

    def __init__(
        self,
        record: Path | None = None,
        table: Callable[[], Table] = process_table,
        end_process: Callable[[int, float], None] = kill,
    ) -> None:
        self.record, self.table, self.end_process = record, table, end_process
        self.known: dict[int, float] = {}
        if record is not None and record.exists():
            saved = json.loads(record.read_text(encoding="utf-8"))
            self.known = {int(pid): float(born) for pid, born in saved.items()}

    def add(self, pid: int) -> None:
        entry = self.table().get(pid)
        if entry is not None:
            self.known[pid] = entry[1]
            self._save()

    def observe(self) -> tuple[int, ...]:
        """Adopt descendants that exist now; returns the newly adopted PIDs."""
        try:
            return self._adopt(self.table())
        except psutil.Error:
            return ()  # the next observation adopts them; an end observes first

    def _adopt(self, table: Table) -> tuple[int, ...]:
        adopted: list[int] = []
        grown = True
        while grown:
            grown = False
            for pid, (parent, created) in table.items():
                if pid in self.known or parent not in self.known:
                    continue
                born = self.known[parent]
                current = table.get(parent)
                if current is not None and abs(current[1] - born) >= SAME_PROCESS:
                    continue  # the parent PID now names another process
                if created + SAME_PROCESS < born:
                    continue  # older than the parent: not its child
                self.known[pid] = created
                adopted.append(pid)
                grown = True
        if adopted:
            self._save()
        return tuple(adopted)

    def alive(self, table: Table) -> list[tuple[int, float]]:
        return [
            (pid, born)
            for pid, born in self.known.items()
            if pid in table and abs(table[pid][1] - born) < SAME_PROCESS
        ]

    def end(self, timeout: float = 5) -> None:
        """End every remembered process and confirm none is left, or raise."""
        deadline = time.monotonic() + timeout
        while True:
            table = self.table()
            self._adopt(table)
            survivors = self.alive(table)
            if not survivors:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Descendants outside containment have not terminated")
            for pid, born in survivors:
                self.end_process(pid, born)
            time.sleep(0.05)

    def _save(self) -> None:
        if self.record is not None:
            atomic_write(self.record, json.dumps({str(k): v for k, v in self.known.items()}))
