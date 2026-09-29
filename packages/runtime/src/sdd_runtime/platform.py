"""OS-specific process containment stays outside the kernel contracts."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any, Protocol

import psutil

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def process_group(pid: int) -> int:
    if sys.platform == "win32":
        raise RuntimeError("POSIX process groups are unavailable on Windows")
    return os.getpgid(pid)


def kill_group(group: int) -> None:
    if sys.platform == "win32":
        raise RuntimeError("POSIX process groups are unavailable on Windows")
    os.killpg(group, signal.SIGKILL)


class Containment(Protocol):
    def assign(self, pid: int) -> None: ...
    def terminate(self) -> None: ...
    def stop_and_confirm(self, timeout: float = 5) -> None: ...
    def close(self) -> None: ...
    def forget(self, pid: int) -> None: ...


class ProcessGroups:
    """POSIX cooperative containment, not a sandbox against setsid/double-fork.

    Hosts must be session leaders. Capabilities deliberately do not claim hard
    memory/CPU limits or containment of a program escaping its process group.
    """

    def __init__(self) -> None:
        self.groups: set[int] = set()

    def assign(self, pid: int) -> None:
        if process_group(pid) != pid:
            raise RuntimeError("Host must start in its own POSIX session")
        self.groups.add(pid)

    def terminate(self) -> None:
        for group in self.groups:
            try:
                kill_group(group)
            except ProcessLookupError:
                pass

    def stop_and_confirm(self, timeout: float = 5) -> None:
        self.terminate()
        deadline = time.monotonic() + timeout
        while any(group_alive(group) for group in self.groups):
            if time.monotonic() >= deadline:
                raise TimeoutError("POSIX group termination is unconfirmed")
            time.sleep(0.01)

    def close(self) -> None:
        self.stop_and_confirm()
        self.groups.clear()

    def forget(self, pid: int) -> None:
        self.groups.discard(pid)


def group_alive(group: int) -> bool:
    for process in psutil.process_iter():
        try:
            if process_group(process.pid) == group and process.status() != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass
        except (PermissionError, psutil.AccessDenied):
            return True
    return False


def Job() -> Containment:
    if sys.platform == "win32":
        from sdd_runtime.windows import Job as WindowsJob

        return WindowsJob()
    return ProcessGroups()


def start_contained(
    argv: Sequence[str], jobs: Sequence[Containment], **options: Any
) -> subprocess.Popen[bytes]:
    """Start `argv` so that it runs no instruction before every job holds it.

    On Windows the process starts suspended. Otherwise a launcher such as a venv
    `python.exe` starts the real interpreter before assignment; when the parent's
    job allows silent breakaway, that child and everything it starts stay outside
    the job and outlive a confirmed stop.
    """
    if sys.platform != "win32":
        process = subprocess.Popen(argv, start_new_session=True, **options)
        try:
            for job in jobs:
                job.assign(process.pid)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
        return process
    from sdd_runtime.windows import CREATE_SUSPENDED, resume

    flags = options.pop("creationflags", NO_WINDOW) | CREATE_SUSPENDED
    process = subprocess.Popen(argv, creationflags=flags, **options)
    try:
        for job in jobs:
            job.assign(process.pid)
        resume(process.pid)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    return process
