"""Bounded installation/model discovery. It never authenticates or calls a model."""

import glob
import os
import shutil
import subprocess
from collections.abc import Callable
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
    probes = []
    for argv in sorted(candidates):
        try:
            process = subprocess.run(
                [*argv, "--version"], capture_output=True, timeout=5, creationflags=NO_WINDOW
            )
            version = process.stdout.decode("utf-8", errors="replace").strip()[:512]
            probes.append(
                InstallationProbe(
                    argv,
                    version if process.returncode == 0 else None,
                    None if process.returncode == 0 else f"exit {process.returncode}",
                )
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            probes.append(InstallationProbe(argv, None, type(error).__name__))
    status = (
        "missing"
        if not probes
        else "ambiguous"
        if len(probes) > 1
        else "installed"
        if probes[0].error is None
        else "broken"
    )
    return DiscoveryResult(spec.id, status, tuple(probes))


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
