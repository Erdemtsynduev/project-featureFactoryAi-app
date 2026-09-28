"""Local composition root: concrete handlers, providers and storage are chosen here only.

The CLI and the UI application both compose through this module, so use cases
never import concrete adapters and neither front end imports the other.
"""

import time
from pathlib import Path

from sdd_core.codec import mapping, object_json, sequence, text
from sdd_core.sdk import Handler, Registry
from sdd_storage.store import Store

from sdd_runtime.engine import Engine
from sdd_runtime.plugins import load_extensions


def local_engine(database: Path) -> Engine:
    """SQLite state, Git revisions and local files."""
    return Engine(Store(database))


def registry(config: Path | None) -> Registry:
    """Handlers for the configured agent profiles, checks and lane operations."""
    from sdd_providers.catalog import adapters
    from sdd_providers.handlers import CommandHandler
    from sdd_providers.structured_cli import Invocation

    from sdd_runtime.lanes import LaneHandler
    from sdd_runtime.profiles import RunnerInstallation, load_profiles, register_profiles
    from sdd_runtime.rotation import Cooldowns, apply_rotations, load_rotations

    result = Registry()
    result.register(CommandHandler())
    for action in ("integrate", "rebase"):
        result.register(LaneHandler(action))
    if config is None:
        return result
    doc = object_json(config.read_text(encoding="utf-8"))
    catalog = adapters(
        tuple(text(x, "extension") for x in sequence(doc.get("agent_extensions", [])))
    )

    def factory(installation: RunnerInstallation, model: str) -> Handler:
        if installation.adapter not in catalog:
            raise ValueError(f"Unknown agent adapter: {installation.adapter}")
        return catalog[installation.adapter].factory(
            Invocation(installation.executable, installation.arguments), model
        )

    if "profiles" in doc:
        profiles = load_profiles(config)
        for profile in profiles.profiles:
            adapter_id = profiles.runners[profile.runner].adapter
            if adapter_id not in catalog:
                raise ValueError(f"Unknown agent adapter: {adapter_id}")
            profile_adapter = catalog[adapter_id]
            if profile.permissions == "read-only" and not profile_adapter.read_only:
                raise ValueError(f"{profile_adapter.id} read-only policy is not qualified")
        register_profiles(result, profiles, factory)
        rotations = load_rotations(doc)
        if rotations:
            apply_rotations(
                result, rotations, Cooldowns(config.with_suffix(".cooldowns.json")), time.time
            )
    else:
        for raw in sequence(doc.get("providers", [])):
            item = mapping(raw)
            adapter = text(item.get("id"), "id")
            if adapter not in catalog:
                raise ValueError(f"Unknown agent adapter: {adapter}")
            model = item.get("model")
            handler = catalog[adapter].factory(
                Invocation(
                    text(item.get("executable"), "executable"),
                    tuple(text(arg, "argument") for arg in sequence(item.get("arguments", []))),
                ),
                None if model is None else text(model, "model"),
            )
            result.register(handler, name=text(item.get("name", adapter), "name"))
        load_extensions(
            result, tuple(text(x, "extension") for x in sequence(doc.get("extensions", [])))
        )
    return result
