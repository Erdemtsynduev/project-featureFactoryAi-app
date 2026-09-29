"""An agent's PATH with packaged-app aliases resolved to the interpreters they launch.

On Windows, `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe` is an app execution
alias of the Python install manager. It runs the real interpreter inside the
package's own job, which lets that interpreter's children break away from every job;
a sandbox then only catches them through its lineage. Asking the alias once which
interpreter it starts, and putting that interpreter's folder ahead of the aliases,
gives the agent the very same Python without the breakaway.
"""

import os
import subprocess
from collections.abc import Callable
from functools import cache
from pathlib import Path

from sdd_runtime.platform import NO_WINDOW

ALIASES = ("python.exe", "python3.exe")


def is_alias_folder(entry: str) -> bool:
    return Path(entry).name.lower() == "windowsapps"


@cache
def launched_by(alias: str) -> str | None:
    """The folder of the interpreter `alias` starts, or None when it cannot be told."""
    try:
        answer = subprocess.run(
            [alias, "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if answer.returncode:
        return None
    executable = Path(answer.stdout.decode(errors="replace").strip())
    if not executable.is_file() or is_alias_folder(str(executable.parent)):
        return None
    return str(executable.parent)


def contained_path(path: str, resolve: Callable[[str], str | None] = launched_by) -> str:
    """`path` with each alias folder preceded by the folders its interpreters live in."""
    if os.name != "nt":
        return path
    entries = [entry for entry in path.split(os.pathsep) if entry]
    result: list[str] = []
    for entry in entries:
        if is_alias_folder(entry):
            for name in ALIASES:
                alias = Path(entry) / name
                real = resolve(str(alias)) if alias.exists() else None
                if real is not None and real not in result:
                    result.append(real)
        if entry not in result:
            result.append(entry)
    return os.pathsep.join(result)
