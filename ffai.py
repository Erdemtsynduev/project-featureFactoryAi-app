"""Repository launcher. Works from any current directory and from paths with spaces."""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print(f'Install first: python "{root / "install.py"}"', file=sys.stderr)
        return 2
    try:
        return subprocess.call([str(python), "-m", "sdd_runtime.cli", *sys.argv[1:]])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
