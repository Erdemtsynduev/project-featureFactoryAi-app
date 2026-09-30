"""Build every wheel and verify imports/CLI in a non-editable isolated install."""

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import annotations
from wheels import DIST, ROOT, clean


def run(args: list[str], cwd: Path = ROOT) -> None:
    """Run one step; on GitHub Actions a failed step is an annotation with its output."""
    try:
        done = subprocess.run(
            args, cwd=cwd, timeout=180, capture_output=True, text=True, errors="replace"
        )
        output, code = done.stdout + done.stderr, done.returncode
    except subprocess.TimeoutExpired as late:
        output, code = f"No result within {late.timeout} s", 1
    print(output, end="")
    if code:
        if annotations.enabled():
            print(annotations.error("build_check: " + " ".join(args[1:4]), output))
        raise SystemExit(code)


clean()
for path in [
    ROOT / "packages" / name
    for name in (
        "core",
        "storage",
        "runtime",
        "providers",
        "workflows",
        "usage",
        "factory",
        "trackers",
        "ui",
    )
] + [ROOT, ROOT / "examples/extension"]:
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(DIST),
            str(path),
        ]
    )
with tempfile.TemporaryDirectory(prefix="sdd-wheel-check-") as temp:
    folder = Path(temp)
    venv.create(folder / "venv", with_pip=True)
    python = folder / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    run(
        [str(python), "-m", "pip", "install", "--find-links", str(DIST), "sdd-runtime==0.5.0"],
        folder,
    )
    run(
        [
            str(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('sdd_storage') is None; from sdd_runtime.application import ApplicationEngine; from sdd_runtime.coordinator import Coordinator; from sdd_runtime.execution import ExecutionDriver; from sdd_runtime.portfolio import PortfolioService; from sdd_runtime.maintenance import archive_diagnostics",
        ],
        folder,
    )
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--find-links",
            str(DIST),
            "feature-factory-ai==0.5.0",
            "sdd-example-extension==0.5.0",
        ],
        folder,
    )
    launcher = python.parent / ("ffai.exe" if os.name == "nt" else "ffai")
    assert (python.parent / ("ffai-app.exe" if os.name == "nt" else "ffai-app")).is_file()
    assert not (python.parent / ("sdd.exe" if os.name == "nt" else "sdd")).exists()
    assert not (python.parent / ("ff.exe" if os.name == "nt" else "ff")).exists()
    run([str(launcher), "--version"], folder)
    run([str(launcher), "ui", "--help"], folder)
    run([str(python), str(ROOT / "tools/ui_smoke.py")], folder)
    run(
        [
            str(python),
            "-c",
            "from importlib.resources import files; from sdd_ui.server import create_server; assert files('sdd_ui').joinpath('static', 'js', 'main.js').is_file(); assert files('sdd_ui').joinpath('static', 'css', 'app.css').is_file(); assert files('sdd_ui').joinpath('static', 'index.html').is_file()",
        ],
        folder,
    )
    run([str(launcher), "demo", "--directory", str(folder / "demo space")], folder)
    run([str(python), "-m", "sdd_runtime.cli", "--help"], folder)
    run(
        [
            str(python),
            "-c",
            "from sdd_core.sdk import Registry; from sdd_runtime.plugins import load_extensions; r=Registry(); load_extensions(r,('example',)); assert r.get('example').manifest.api_version == 1",
        ],
        folder,
    )
print("Isolated wheel installation: PASS")
