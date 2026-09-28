"""Explicit agent adapter catalog. Loading a third-party adapter is operator opt-in."""

import json
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
    auth_arguments: tuple[str, ...] | None = None
    parse_auth: Callable[[int, str], tuple[str, str]] | None = None
    suggested_models: tuple[str, ...] = ()


def _cli(provider: str, invocation: Invocation, model: str | None) -> Handler:
    if invocation.arguments:
        raise ValueError(f"{provider} adapter requires a native executable")
    return CliHandler(provider, invocation.executable, model)


def _opencode(invocation: Invocation, model: str | None) -> Handler:
    if model is not None and "/" not in model:
        raise ValueError("OpenCode model must be the exact provider/model identifier")
    return StructuredCliHandler("opencode", invocation, model)


def _claude_auth(code: int, output: str) -> tuple[str, str]:
    start = output.find("{")
    try:
        state = json.loads(output[start : output.rfind("}") + 1]) if start >= 0 else {}
    except ValueError:
        state = {}
    if not isinstance(state, dict) or "loggedIn" not in state:
        return "unknown", "Unrecognized status output"
    if state["loggedIn"] is not True:
        return "not_authenticated", "Run: claude auth login"
    parts = [str(state.get(key)) for key in ("authMethod", "subscriptionType") if state.get(key)]
    return "authenticated", " · ".join(parts)


def _phrase_auth(login_hint: str) -> Callable[[int, str], tuple[str, str]]:
    def parse(code: int, output: str) -> tuple[str, str]:
        lowered = output.lower()
        if "not logged in" in lowered or "logged out" in lowered:
            return "not_authenticated", login_hint
        if code == 0 and "logged in" in lowered:
            line = next((x.strip("✓ ").strip() for x in output.splitlines() if x.strip()), "")
            # Keep the login method, never the account identifier.
            return "authenticated", line.split(" as ")[0].split(" using ")[-1][:80]
        return "unknown", f"exit {code}"

    return parse


def _opencode_auth(code: int, output: str) -> tuple[str, str]:
    stored = [line for line in output.splitlines() if line.rstrip().endswith("stored")]
    if code == 0 and stored:
        return "authenticated", f"{len(stored)} stored credential(s)"
    return (
        ("not_authenticated", "Run: opencode auth login")
        if code == 0
        else ("unknown", f"exit {code}")
    )


def adapters(extensions: tuple[str, ...] = ()) -> dict[str, AgentAdapter]:
    result = {
        "claude": AgentAdapter(
            "claude",
            ("claude",),
            lambda i, m: _cli("claude", i, m),
            auth_arguments=("auth", "status"),
            parse_auth=_claude_auth,
            suggested_models=("opus", "sonnet", "haiku"),
        ),
        "codex": AgentAdapter(
            "codex",
            ("codex",),
            lambda i, m: _cli("codex", i, m),
            auth_arguments=("login", "status"),
            parse_auth=_phrase_auth("Run: codex login"),
        ),
        "cursor": AgentAdapter(
            "cursor",
            ("agent", "cursor-agent"),
            lambda i, m: StructuredCliHandler("cursor", i, m),
            ("models",),
            True,
            ("$LOCALAPPDATA/cursor-agent/versions/*/node.exe",),
            "index.js",
            cursor_models,
            ("status",),
            _phrase_auth("Run: cursor-agent login"),
        ),
        "opencode": AgentAdapter(
            "opencode",
            ("opencode",),
            _opencode,
            ("models", "--standalone"),
            False,
            ("$APPDATA/npm/node_modules/@opencode/cli/bin/opencode.exe",),
            parse_models=opencode_models,
            auth_arguments=("auth", "list"),
            parse_auth=_opencode_auth,
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
