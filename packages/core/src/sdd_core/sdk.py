"""Explicit plugin registration. Plugins describe capabilities, not acceptance authority."""

from dataclasses import dataclass, replace
from typing import Protocol

from sdd_core.models import Attempt, Result, Step

API_VERSION = 1
# What a launched payload writes in its attempt folder; the host opens both right
# before the payload starts, so their presence proves the launch.
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"


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
