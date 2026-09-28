"""Build every wheel and verify imports/CLI in a non-editable isolated install."""

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from wheels import DIST, ROOT, clean


def run(args: list[str], cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, check=True, timeout=180)


clean()
for path in [
    ROOT / "packages" / name
    for name in ("core", "storage", "runtime", "providers", "workflows", "usage", "ui")
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
        [str(python), "-m", "pip", "install", "--find-links", str(DIST), "sdd-runtime==0.1.0"],
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
            "feature-factory-ai==0.1.0",
            "sdd-example-extension==0.1.0",
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
