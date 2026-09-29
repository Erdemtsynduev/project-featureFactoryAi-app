"""Gated host processes: spawn under containment, settle, and probe ownership after restart."""

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil
from sdd_core.sdk import Packet

from sdd_runtime.platform import Containment, Job, group_alive, start_contained

# Caps of the aggregate Windows job (see windows.py); reported in the health file.
RESOURCE_LIMITS = {"cpu_percent": 50, "memory_mb": 16384, "processes": 128}


@dataclass
class Running:
    packet: Packet
    process: subprocess.Popen[bytes]
    job: Containment
    nonce: str


class Hosts:
    """Owns the aggregate resource job and every per-attempt job it creates."""

    def __init__(self) -> None:
        self.resource_job = Job()

    def spawn(self, packet: Packet, nonce: str, started: Callable[[int, float], None]) -> Running:
        """Start a host waiting on stdin; `started` persists its identity before GO."""
        job = Job()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = start_contained(
                [sys.executable, "-m", "sdd_runtime.host", str(Path(packet.directory))],
                [self.resource_job, job],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            started(process.pid, psutil.Process(process.pid).create_time())
            if process.stdin is None:
                raise RuntimeError("Missing host gate")
            process.stdin.write(b"GO\n")
            process.stdin.close()
            return Running(packet, process, job, nonce)
        except BaseException:
            job.close()
            if process:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                self.resource_job.forget(process.pid)
            raise

    def settle(self, running: Running, *, wait: bool) -> None:
        """Confirm the attempt's job is empty and release it; `wait` reaps a stopped host."""
        running.job.stop_and_confirm()
        if wait:
            running.process.wait(timeout=10)
        running.job.close()
        self.resource_job.forget(running.process.pid)

    def release(self, running: Running) -> None:
        """Shutdown: close the job (terminating its tree) and reap the host."""
        running.job.close()
        running.process.wait(timeout=10)
        self.resource_job.forget(running.process.pid)

    @staticmethod
    def ended(pid: int | None, created: float | None) -> bool:
        """Whether a recorded host is certainly gone. Unknown ownership is never 'ended'."""
        if pid is None:
            return True
        confirmed = False
        if created is not None:
            try:
                process = psutil.Process(int(pid))
                confirmed = abs(process.create_time() - created) > 0.01
                if not confirmed:
                    try:
                        process.wait(timeout=2)
                        confirmed = True
                    except psutil.TimeoutExpired:
                        pass
            except psutil.NoSuchProcess:
                confirmed = True
            except psutil.AccessDenied:
                confirmed = False
        if os.name != "nt" and group_alive(int(pid)):
            confirmed = False
        return confirmed

    def close(self) -> None:
        self.resource_job.close()


def health(live: tuple[str, ...], event: float | None, now: float) -> dict[str, object]:
    return {
        "coordinator_pid": os.getpid(),
        "heartbeat": now,
        "active_attempts": sorted(live),
        "last_transition": event,
        "resource_limits": RESOURCE_LIMITS if os.name == "nt" else None,
        "containment": "windows-job" if os.name == "nt" else "cooperative-posix-group",
    }
