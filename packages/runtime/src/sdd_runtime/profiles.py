"""Configuration composition; no provider identifiers are hard-coded here."""

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast

from sdd_core.codec import canonical, integer, mapping, object_json, sequence, text
from sdd_core.models import Result
from sdd_core.profiles import AgentProfile
from sdd_core.sdk import Handler, Launch, Manifest, Packet, Registry


@dataclass(frozen=True)
class RunnerInstallation:
    adapter: str
    executable: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileConfiguration:
    runners: dict[str, RunnerInstallation]
    profiles: tuple[AgentProfile, ...]


class HandlerFactory(Protocol):
    def __call__(self, installation: RunnerInstallation, model: str) -> Handler: ...


def load_profiles(path: Path) -> ProfileConfiguration:
    document = object_json(path.read_text(encoding="utf-8"))
    if document.get("schema") != 1 or document.keys() - {
        "schema",
        "runners",
        "profiles",
        "agent_extensions",
    }:
        raise ValueError("Unsupported profiles configuration")
    runners = {}
    for name, raw in mapping(document.get("runners")).items():
        item = mapping(raw)
        if item.keys() - {"adapter", "executable", "arguments"}:
            raise ValueError("Unknown runner fields")
        runners[name] = RunnerInstallation(
            text(item.get("adapter"), "adapter"),
            text(item.get("executable"), "executable"),
            tuple(text(arg, "argument") for arg in sequence(item.get("arguments", []))),
        )
    profiles = []
    for name, raw in mapping(document.get("profiles")).items():
        item = mapping(raw)
        if item.keys() - {"runner", "model", "permissions", "timeout_seconds"}:
            raise ValueError("Unknown profile fields")
        profile = AgentProfile(
            name,
            text(item.get("runner"), "runner"),
            text(item.get("model"), "model"),
            cast(
                Literal["read-only", "workspace-write"],
                text(item.get("permissions", "read-only"), "permissions"),
            ),
            integer(item.get("timeout_seconds", 900), "timeout_seconds"),
        )
        if profile.runner not in runners:
            raise ValueError(f"Profile {name} references an unknown installation")
        profiles.append(profile)
    return ProfileConfiguration(runners, tuple(profiles))


class ProfileHandler:
    def __init__(self, profile: AgentProfile, delegate: Handler) -> None:
        self.profile, self.delegate = profile, delegate
        self.manifest: Manifest = replace(
            delegate.manifest,
            id=profile.name,
            settings=canonical(
                {"profile": asdict(profile), "adapter": object_json(delegate.manifest.settings)}
            ),
        )

    def prepare(self, packet: Packet) -> Launch:
        if packet.step.mutates and self.profile.permissions == "read-only":
            raise ValueError("Profile denies workspace mutation")
        return self.delegate.prepare(packet)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        return self.delegate.collect(packet, exit_code, revision)


def register_profiles(
    registry: Registry, config: ProfileConfiguration, factory: HandlerFactory
) -> None:
    for profile in config.profiles:
        registry.register(
            ProfileHandler(profile, factory(config.runners[profile.runner], profile.model))
        )
