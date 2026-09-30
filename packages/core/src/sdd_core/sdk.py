"""Explicit plugin registration. Plugins describe capabilities, not acceptance authority."""

from dataclasses import dataclass, replace
from typing import Protocol

from sdd_core.models import Attempt, Result, Step

API_VERSION = 1
# What a launched payload writes in its attempt folder; the host opens both right
# before the payload starts, so their presence proves the launch.
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
# The attempt packet the engine writes before launch, for handlers that run later.
PACKET_FILE = "packet.json"
# The name an attempt's own files go by as evidence. They are the engine's files, not
# the project's: the engine keeps them in its own folder, outside every workspace.
ENGINE_DIRECTORY = ".sdd-engine"


def _within(path: str, root: str) -> str | None:
    """`path` relative to `root`, or None when it is not inside it."""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def evidence_name(path: str, workspace: str, folder: str) -> str:
    """The name a file is recorded by as evidence. The three paths are absolute,
    resolved and written with forward slashes.

    A file of the attempt's folder `<...>/<run>/<attempt>` is named
    `.sdd-engine/<run>/<attempt>/<file>`, wherever the engine keeps that folder. Any
    other file must be in the workspace and is named relative to it.
    """
    inside = _within(path, folder)
    if inside is not None:
        run, attempt = folder.rstrip("/").split("/")[-2:]
        return "/".join((ENGINE_DIRECTORY, run, attempt, inside))
    inside = _within(path, workspace)
    if inside is None:
        raise ValueError("Evidence must be a file of the workspace or of the attempt")
    return inside


def handler_key(step: Step) -> str:
    if not step.handler and step.profile != "default":
        return step.profile
    return step.handler if step.profile == "default" else f"{step.handler}/{step.profile}"


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    api_version: int = API_VERSION
    capabilities: tuple[str, ...] = ()
    config_schema: str = '{"type":"object"}'
    settings: str = "{}"


@dataclass(frozen=True)
class Packet:
    run_id: str
    attempt: Attempt
    step: Step
    workspace: str
    directory: str
    context: str
    # Native session to continue; context then holds only what is new since it.
    resume: str = ""
    # Repositories (workspace-relative) that the run's accepted prerequisites changed:
    # a dependent repository advances its pins to what they delivered.
    delivered: tuple[str, ...] = ()


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...] = ()
    result_file: str = "result.json"
    # Text the process reads on stdin. A prompt goes here, not in argv: Windows caps
    # a whole command line at 32,767 characters.
    input: str = ""


class Handler(Protocol):
    manifest: Manifest

    def prepare(self, packet: Packet) -> Launch: ...

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result: ...


class ProjectAdapter(Protocol):
    manifest: Manifest

    def revision(self, workspace: str) -> str: ...


class Registry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, handler: Handler, name: str | None = None) -> None:
        manifest = handler.manifest
        key = name or manifest.id
        if manifest.api_version != API_VERSION or key in self._handlers:
            raise ValueError("Incompatible or duplicate plugin")
        self._handlers[key] = handler

    def replace(self, handler: Handler, name: str) -> None:
        """Swap a registered handler for a composed one, e.g. an agent rotation."""
        if name not in self._handlers or handler.manifest.api_version != API_VERSION:
            raise ValueError("Only a registered, compatible handler can be replaced")
        self._handlers[name] = handler

    def get(self, identifier: str) -> Handler:
        return self._handlers[identifier]

    def manifests(self) -> tuple[Manifest, ...]:
        return tuple(
            replace(self._handlers[key].manifest, id=key) for key in sorted(self._handlers)
        )
