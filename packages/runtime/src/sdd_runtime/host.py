"""A gated process host. EOF before GO means the launcher died: execute nothing."""

import os
import subprocess
import sys
import time
from pathlib import Path

from sdd_core.codec import canonical, integer, mapping, object_json, sequence, text

from sdd_runtime.files import atomic_write
from sdd_runtime.platform import NO_WINDOW
from sdd_runtime.watchdog import watch_parent


def main() -> int:
    # Start before reading GO, so parent death between GO and payload is covered.
    folder = Path(sys.argv[1]).resolve()
    config = object_json((folder / "launch.json").read_text(encoding="utf-8"))
    watch_parent(integer(config["parent_pid"], "parent_pid") if "parent_pid" in config else None)
    if sys.stdin.readline().strip() != "GO":
        return 2
    argv = [text(x, "argv") for x in sequence(config["argv"])]
    environment = os.environ.copy()
    environment.update(
        {key: text(value, "environment") for key, value in mapping(config["environment"]).items()}
    )
    with (folder / "stdout.log").open("wb") as out, (folder / "stderr.log").open("wb") as err:
        result = subprocess.run(
            argv,
            cwd=text(config["cwd"], "cwd"),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=NO_WINDOW,
        )
    atomic_write(
        folder / "exit.json",
        canonical(
            {"exit_code": result.returncode, "nonce": config["nonce"], "completed_at": time.time()}
        ),
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
