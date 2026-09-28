"""Reproducible local checks; never labels missing OS or 24-hour runs as passed."""

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

from soak import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


class CheckResult(TypedDict):
    name: str
    command: list[str]
    exit_code: int
    elapsed: float


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ROOT / "reports/refactor-qualification")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    folder = args.directory.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    source = source_fingerprint()
    commands = [
        ("boundaries", ["tools/check_boundaries.py"]),
        ("lint", ["-m", "ruff", "check", "."]),
        ("format", ["-m", "ruff", "format", "--check", "."]),
        ("types-local", ["-m", "mypy"]),
        ("types-linux", ["-m", "mypy", "--platform", "linux"]),
        ("types-macos", ["-m", "mypy", "--platform", "darwin"]),
        ("tests", ["-m", "pytest", "-q", "--junitxml=" + str(folder / "tests.xml")]),
    ]
    if args.build:
        commands.append(("wheels", ["tools/build_check.py"]))
    checks: list[CheckResult] = []
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "source_sha256": source,
        "started": time.time(),
        "checks": checks,
        "qualification": {"other_operating_systems": "not_run", "24_hours": "not_completed"},
    }
    for name, arguments in commands:
        command = [sys.executable, *arguments]
        started = time.monotonic()
        with (folder / f"{name}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=600
            )
        checks.append(
            {
                "name": name,
                "command": command,
                "exit_code": result.returncode,
                "elapsed": time.monotonic() - started,
            }
        )
        print(f"{name}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
    passed = all(check["exit_code"] == 0 for check in checks)
    report["source_unchanged"] = source_fingerprint() == source
    report["status"] = "local_checks_passed" if passed and report["source_unchanged"] else "failed"
    report["finished"] = time.time()
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["status"] == "local_checks_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
