"""Explicit agent adapter catalog. Loading a third-party adapter is operator opt-in."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import cast

from sdd_core.sdk import Handler

from sdd_providers.handlers import CliHandler
from sdd_providers.model_catalog import cursor_models, opencode_models
from sdd_providers.structured_cli import Invocation, StructuredCliHandler


@dataclass(frozen=True)
class AgentAdapter:
    id: str
    commands: tuple[str, ...]
    factory: Callable[[Invocation, str | None], Handler]
    model_arguments: tuple[str, ...] | None = None
    read_only: bool = True
    install_patterns: tuple[str, ...] = ()
    entrypoint: str | None = None
    parse_models: Callable[[str], tuple[str, ...]] | None = None


def _cli(provider: str, invocation: Invocation, model: str | None) -> Handler:
    if invocation.arguments:
        raise ValueError(f"{provider} adapter requires a native executable")
    return CliHandler(provider, invocation.executable, model)


def _opencode(invocation: Invocation, model: str | None) -> Handler:
    if model is not None and "/" not in model:
        raise ValueError("OpenCode model must be the exact provider/model identifier")
    return StructuredCliHandler("opencode", invocation, model)


def adapters(extensions: tuple[str, ...] = ()) -> dict[str, AgentAdapter]:
    result = {
        "claude": AgentAdapter("claude", ("claude",), lambda i, m: _cli("claude", i, m)),
        "codex": AgentAdapter("codex", ("codex",), lambda i, m: _cli("codex", i, m)),
        "cursor": AgentAdapter(
            "cursor",
            ("agent", "cursor-agent"),
            lambda i, m: StructuredCliHandler("cursor", i, m),
            ("models",),
            True,
            ("$LOCALAPPDATA/cursor-agent/versions/*/node.exe",),
            "index.js",
            cursor_models,
        ),
        "opencode": AgentAdapter(
            "opencode",
            ("opencode",),
            _opencode,
            ("models", "--standalone"),
            False,
            ("$APPDATA/npm/node_modules/@opencode/cli/bin/opencode.exe",),
            parse_models=opencode_models,
        ),
    }
    installed = {point.name: point for point in entry_points(group="sdd.agents")}
    for name in extensions:
        if name not in installed:
            raise ValueError(f"Agent adapter not installed: {name}")
        adapter = cast(AgentAdapter, installed[name].load()())
        if adapter.id in result:
            raise ValueError("Duplicate agent adapter")
        result[adapter.id] = adapter
    return result
