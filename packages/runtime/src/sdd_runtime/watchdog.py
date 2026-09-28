"""Cooperative POSIX parent-death guard for session-leading engine hosts."""

import os
import threading
import time

from sdd_runtime.platform import kill_group, process_group


def watch_parent(expected: int | None = None) -> None:
    if os.name == "nt":
        return
    parent = os.getppid() if expected is None else expected
    if process_group(0) != os.getpid():
        raise RuntimeError("Parent guard requires a dedicated process group")

    def monitor() -> None:
        while True:
            if os.getppid() != parent:
                kill_group(os.getpid())
                return
            time.sleep(0.1)

    threading.Thread(target=monitor, name="sdd-parent-guard", daemon=True).start()
