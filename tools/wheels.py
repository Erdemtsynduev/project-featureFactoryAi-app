"""Build every wheel from a clean tree.

setuptools reuses `build/lib` between builds, so a module deleted from the
sources would still ship in the next wheel. Always build through this script:

    python tools/wheels.py
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def clean() -> None:
    """Remove intermediate build trees and previous wheels (all ignored by Git)."""
    for folder in (
        ROOT / "build",
        *ROOT.glob("packages/*/build"),
        ROOT / "examples/extension/build",
    ):
        if folder.is_dir():
            shutil.rmtree(folder)
    for wheel in DIST.glob("*.whl"):
        wheel.unlink()


def main() -> None:
    clean()
    subprocess.run(
        ["uv", "build", "--all-packages", "--wheel", "--out-dir", str(DIST)],
        cwd=ROOT,
        check=True,
        timeout=600,
    )


if __name__ == "__main__":
    main()
