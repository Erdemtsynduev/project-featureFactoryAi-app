"""Install Feature Factory AI locally with only Python; no activation required."""

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev", action="store_true", help="Also install checks and example extension"
    )
    args = parser.parse_args()
    if sys.version_info < (3, 12):  # noqa: UP036 - bootstrap runs before package installation
        parser.error("Python 3.12 or newer is required")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.create(environment, with_pip=True)
    packages = [
        root / "packages" / name
        for name in ("core", "storage", "runtime", "providers", "workflows")
    ]
    command = [str(python), "-m", "pip", "install"]
    # One resolver transaction uses local packages rather than similarly named index packages.
    for package in packages:
        command.extend(("-e", str(package)))
    command.extend(("-e", str(root) + ("[dev]" if args.dev else "")))
    if args.dev:
        command.extend(("-e", str(root / "examples/extension")))
    subprocess.run(command, cwd=root, check=True)
    subprocess.run([str(python), "-m", "sdd_runtime.cli", "--version"], cwd=root, check=True)
    if os.name != "nt":
        (root / "ffai").chmod((root / "ffai").stat().st_mode | 0o111)
    print("Ready. Run .\\ffai --help on Windows or ./ffai --help on Linux/macOS.")
    print("Try .\\ffai demo (Windows) or ./ffai demo (Linux/macOS). No model calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
