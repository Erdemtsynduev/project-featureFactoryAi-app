"""Bounded installation/model discovery. It never authenticates or calls a model."""

import glob
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sdd_runtime.platform import NO_WINDOW


class DiscoverySpec(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def commands(self) -> tuple[str, ...]: ...
    @property
    def install_patterns(self) -> tuple[str, ...]: ...
    @property
    def entrypoint(self) -> str | None: ...
    @property
    def auth_arguments(self) -> tuple[str, ...] | None: ...
    @property
    def parse_auth(self) -> Callable[[int, str], tuple[str, str]] | None: ...


@dataclass(frozen=True)
class InstallationProbe:
    argv: tuple[str, ...]
    version: str | None
    error: str | None


@dataclass(frozen=True)
class DiscoveryResult:
    adapter: str
    status: str
    candidates: tuple[InstallationProbe, ...]
    authentication: str = "unknown"
    qualification: str = "not_checked"
    selected: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Authentication:
    """Native CLI login state. `detail` never carries account identifiers."""

    status: str  # authenticated | not_authenticated | unknown
    detail: str = ""


def _versions_of_one_install(candidates: list[InstallationProbe]) -> InstallationProbe | None:
    """Side-by-side versions under one versions/ folder: the newest working one wins."""
    parents = {Path(c.argv[0]).parent.parent for c in candidates}
    if len(parents) != 1 or next(iter(parents)).name.lower() != "versions":
        return None
    working = [c for c in candidates if c.error is None]
    return max(working, key=lambda c: Path(c.argv[0]).parent.name) if working else None


def discover(spec: DiscoverySpec) -> DiscoveryResult:
    candidates: set[tuple[str, ...]] = set()
    for command in spec.commands:
        path = shutil.which(command)
        if path and Path(path).suffix.lower() not in (".ps1", ".cmd", ".bat"):
            candidates.add((str(Path(path).resolve()),))
    for pattern in spec.install_patterns:
        for raw in glob.glob(os.path.expanduser(os.path.expandvars(pattern))):
            executable = Path(raw).resolve()
            prefix: tuple[str, ...] = (str(executable),)
            if spec.entrypoint:
                entry = executable.parent / spec.entrypoint
                if not entry.is_file():
                    continue
                prefix += (str(entry),)
            candidates.add(prefix)

    def probe(argv: tuple[str, ...]) -> InstallationProbe:
        try:
            process = subprocess.run(
                [*argv, "--version"], capture_output=True, timeout=5, creationflags=NO_WINDOW
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return InstallationProbe(argv, None, type(error).__name__)
        version = process.stdout.decode("utf-8", errors="replace").strip()[:512]
        return InstallationProbe(
            argv,
            version if process.returncode == 0 else None,
            None if process.returncode == 0 else f"exit {process.returncode}",
        )

    # Side-by-side version folders can hold dozens of copies; probe them concurrently.
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(probe, sorted(candidates)))
    newest = _versions_of_one_install(probes) if len(probes) > 1 else None
    status = (
        "missing"
        if not probes
        else "installed"
        if newest is not None
        else "ambiguous"
        if len(probes) > 1
        else "installed"
        if probes[0].error is None
        else "broken"
    )
    selected = (
        newest.argv if newest is not None else probes[0].argv if status == "installed" else None
    )
    return DiscoveryResult(spec.id, status, tuple(probes), selected=selected)


def authenticate(spec: DiscoverySpec, argv: tuple[str, ...]) -> Authentication:
    """Ask the native CLI for its login state. No model request is made."""
    if spec.auth_arguments is None or spec.parse_auth is None:
        return Authentication("unknown", "No qualified status command")
    try:
        process = subprocess.run(
            [*argv, *spec.auth_arguments],
            capture_output=True,
            timeout=20,
            creationflags=NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Authentication("unknown", type(error).__name__)
    output = (process.stdout + b"\n" + process.stderr).decode("utf-8", errors="replace")
    status, detail = spec.parse_auth(process.returncode, output)
    return Authentication(status, detail)


def list_models(
    argv: tuple[str, ...],
    arguments: tuple[str, ...] | None,
    parser: Callable[[str], tuple[str, ...]] | None,
) -> tuple[str, ...]:
    if arguments is None or parser is None:
        raise ValueError("This adapter has no qualified model-list command")
    response = subprocess.run(
        [*argv, *arguments], capture_output=True, timeout=15, creationflags=NO_WINDOW
    )
    if response.returncode:
        raise ValueError(f"Model catalog unavailable: exit {response.returncode}")
    content = response.stdout.decode("utf-8", errors="strict")
    models = parser(content)
    if not models:
        raise ValueError(
            "Model catalog returned no recognized identifiers; availability remains unknown"
        )
    return models
