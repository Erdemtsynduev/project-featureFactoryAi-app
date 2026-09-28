"""Portable, immutable agent profiles; machine installation details live elsewhere."""

from dataclasses import asdict, dataclass, replace
from typing import Literal

from sdd_core.codec import canonical, object_json
from sdd_core.models import Workflow


@dataclass(frozen=True)
class AgentProfile:
    name: str
    runner: str
    model: str
    permissions: Literal["read-only", "workspace-write"] = "read-only"
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if (
            not self.name
            or not self.runner
            or not self.model
            or "<" in self.model
            or any(character.isspace() for character in self.model)
            or self.model in {"auto", "default"}
        ):
            raise ValueError("Profile needs a name, runner and exact model identifier")
        if self.permissions not in ("read-only", "workspace-write"):
            raise ValueError("Unknown permission policy")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 86400:
            raise ValueError("Invalid profile timeout")


def resolve_profiles(workflow: Workflow, profiles: tuple[AgentProfile, ...]) -> Workflow:
    catalog = {profile.name: profile for profile in profiles}
    if len(catalog) != len(profiles):
        raise ValueError("Duplicate profile")
    steps = []
    for step in workflow.steps:
        if step.kind != "agent" or step.profile == "default":
            steps.append(step)
            continue
        if step.profile not in catalog:
            raise ValueError(f"Unknown profile: {step.profile}")
        profile = catalog[step.profile]
        if step.mutates and profile.permissions != "workspace-write":
            raise ValueError(f"Read-only profile on mutating step: {step.id}")
        config = object_json(step.config)
        config["profile_snapshot"] = object_json(canonical(asdict(profile)))
        steps.append(
            replace(step, handler="", timeout=profile.timeout_seconds, config=canonical(config))
        )
    return replace(workflow, steps=tuple(steps))
