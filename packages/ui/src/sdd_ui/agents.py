"""Agent profiles, rotation and account quotas: the handlers the queue executes with.

The profiles file is the single source of truth; every change is validated by
building a complete registry from a candidate file before it replaces the old
one, so a bad edit never reaches the running queue.
"""

import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import canonical, integer, mapping, object_json, sequence, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_core.sdk import Registry
from sdd_providers.catalog import adapters
from sdd_runtime.composition import registry
from sdd_runtime.discovery import list_models
from sdd_runtime.files import atomic_write
from sdd_runtime.profiles import load_profiles
from sdd_runtime.rotation import Cooldowns

from sdd_ui.subscriptions import (
    QuotaMonitor,
    claude_credentials,
    read_claude_quota,
    read_codex_quota,
)
from sdd_ui.workspace import WorkspaceCatalog

EMPTY: dict[str, Json] = {"schema": 1, "runners": {}, "profiles": {}}


class AgentSettings:
    def __init__(
        self,
        config: Path,
        catalog: WorkspaceCatalog,
        busy: Callable[[], bool],
        installed: Callable[[Registry], None],
    ) -> None:
        """`busy` reports live executions; `installed` receives every new registry."""
        self.config, self.catalog = config, catalog
        self.busy, self.installed = busy, installed
        self.cooldowns = Cooldowns(config.with_suffix(".cooldowns.json"))
        self.handlers = registry(config if config.exists() else None)
        self.quotas = QuotaMonitor(
            self.cooldowns,
            self.subscription_adapters,
            {
                "claude": lambda now: read_claude_quota(claude_credentials(), now),
                "codex": lambda now: read_codex_quota(self._codex_argv(), now),
            },
        )

    def subscription_adapters(self) -> dict[str, str]:
        """Profile name -> runner adapter; no profiles file means no subscriptions."""
        return load_profiles(self.config).adapters() if self.config.exists() else {}

    def document(self) -> dict[str, Json]:
        if not self.config.exists():
            return dict(EMPTY)
        return object_json(self.config.read_text(encoding="utf-8"))

    def profile_models(self) -> dict[str, str]:
        profiles = mapping(self.document().get("profiles", {}))
        return {name: str(mapping(item).get("model", "")) for name, item in profiles.items()}

    def resting(self) -> dict[str, dict[str, Json]]:
        now = time.time()
        return {
            name: rest
            for name, rest in self.cooldowns.read().items()
            if isinstance(rest.get("until"), (int, float)) and float(str(rest["until"])) > now
        }

    def available(self, name: str) -> bool:
        """A profile (or one member of its rotation) can take work now."""
        rule = mapping(mapping(self.document().get("rotation", {})).get(name, {}))
        members = [name, *(text(x, "fallback") for x in sequence(rule.get("fallbacks", [])))]
        resting = self.resting()
        return any(member not in resting for member in members)

    def install(self, config: Json) -> list[dict[str, object]]:
        if self.busy():
            raise Conflict("Wait for active executions before changing profiles")
        candidate = self.config.with_suffix(".candidate.json")
        try:
            atomic_write(candidate, canonical(config))
            load_profiles(candidate)
            handlers = registry(candidate)
            atomic_write(self.config, candidate.read_text(encoding="utf-8"))
        finally:
            candidate.unlink(missing_ok=True)
        self.handlers = handlers
        self.installed(handlers)
        return [asdict(item) for item in handlers.manifests()]

    def connect(self, doc: dict[str, Json]) -> list[dict[str, object]]:
        """Register a discovered CLI as a runner plus a named profile.

        The profile name is what workflow steps reference as their handler, so
        connecting `claude` and `codex` makes the bundled templates runnable.
        """
        adapter = text(doc.get("adapter"), "adapter")
        argv = [text(x, "argv") for x in sequence(doc.get("argv", []))]
        name = text(doc.get("name", adapter), "name").strip() or adapter
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError("Connect a discovered installation with an absolute executable")
        known = {candidate.argv for candidate in self.catalog.probes.get(adapter, ())}
        if tuple(argv) not in known:
            raise ValueError("Run discovery first; only discovered installations can be connected")
        current = self.document()
        runners = dict(mapping(current.get("runners", {})))
        profiles = dict(mapping(current.get("profiles", {})))
        runners[adapter] = {
            "adapter": adapter,
            "executable": argv[0],
            "arguments": list[Json](argv[1:]),
        }
        profiles[name] = {
            "runner": adapter,
            "model": text(doc.get("model"), "model").strip(),
            "permissions": text(doc.get("permissions", "workspace-write"), "permissions"),
            "timeout_seconds": integer(doc.get("timeout_seconds", 3600), "timeout_seconds"),
        }
        return self.install({**EMPTY, **current, "runners": runners, "profiles": profiles})

    def rotation(self, doc: dict[str, Json]) -> list[dict[str, object]]:
        """Set or clear the fallback chain of one profile."""
        name = text(doc.get("name"), "name")
        current = self.document()
        if name not in mapping(current.get("profiles", {})):
            raise ValueError("Connect the profile before configuring its rotation")
        rules = dict(mapping(current.get("rotation", {})))
        fallbacks = [text(x, "fallback") for x in sequence(doc.get("fallbacks", []))]
        if fallbacks:
            on = [text(x, "failure") for x in sequence(doc.get("on", []))]
            rules[name] = {
                "fallbacks": list[Json](fallbacks),
                "on": list[Json](on) or ["usage_limit", "rate_limit", "authentication"],
                "cooldown_minutes": integer(doc.get("cooldown_minutes", 60), "cooldown_minutes"),
                "retry_seconds": integer(doc.get("retry_seconds", 20), "retry_seconds"),
            }
        else:
            rules.pop(name, None)
        return self.install({**current, "rotation": rules})

    def wake(self, doc: dict[str, Json]) -> dict[str, dict[str, Json]]:
        """End a profile's rest early, e.g. after the operator logged in again."""
        name = text(doc.get("name"), "name")
        state = self.cooldowns.read()
        if name in state:
            self.cooldowns.rest(name, 0.0, "released")
        return self.resting()

    def models(self, adapter_id: str) -> tuple[str, ...]:
        """Model identifiers the installed CLI reports, or its documented aliases."""
        adapter = adapters()[adapter_id]
        found = next((d for d in self.catalog.discovery if d["adapter"] == adapter_id), None)
        selected = found.get("selected") if found else None
        suggestions = list(adapter.suggested_models)
        if adapter_id == "codex":
            configured = Path.home() / ".codex" / "config.toml"
            if configured.is_file():
                model = tomllib.loads(configured.read_text(encoding="utf-8")).get("model")
                if isinstance(model, str) and model:
                    suggestions.insert(0, model)
        if adapter.model_arguments is not None and isinstance(selected, (list, tuple)):
            try:
                listed = list_models(
                    tuple(str(x) for x in selected), adapter.model_arguments, adapter.parse_models
                )
                suggestions.extend(m for m in listed if m not in ("auto", "default"))
            except (ValueError, OSError, UnicodeDecodeError):
                pass
        return tuple(dict.fromkeys(suggestions))

    def _codex_argv(self) -> tuple[str, ...]:
        """The one configured Codex installation (the monitor reads configured adapters)."""
        installed = load_profiles(self.config).installations("codex")
        if len(installed) != 1:
            raise ValueError("Select exactly one Codex installation in agent profiles")
        return (installed[0].executable, *installed[0].arguments)

    def watch_quotas(self, now: float, force: bool = False) -> dict[str, object]:
        """Read due subscriptions, rest spent profiles, publish the overview."""
        self.quotas.refresh(now, force)
        self.catalog.subscription = self.quotas.overview(now)
        return self.catalog.subscription

    def subscription(self) -> dict[str, object]:
        """Every subscription's windows, read now; no model turn."""
        return self.watch_quotas(time.time(), force=True)
