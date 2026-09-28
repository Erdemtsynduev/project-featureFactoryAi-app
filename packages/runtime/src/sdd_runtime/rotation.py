"""Agent rotation: when a profile hits a limit, rest it and hand the step to the next.

Configured in the profiles file:

    "rotation": {
      "codex": {"fallbacks": ["claude"], "on": ["usage_limit", "rate_limit"],
                "cooldown_minutes": 60, "retry_seconds": 20}
    }

Steps keep referring to the primary name (`codex`); the registry serves a
rotation handler under that name. The member chosen for an attempt is written
into the attempt folder before launch, so the decision is recorded before
execution and collection always uses the same member. A resting profile comes
back automatically when its cooldown (or the reset the CLI reported) passes.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from sdd_core.codec import canonical, integer, mapping, object_json, sequence, text
from sdd_core.models import Json, Result
from sdd_core.sdk import Handler, Launch, Packet, Registry

from sdd_runtime.files import atomic_write

FAILURES = ("usage_limit", "rate_limit", "authentication", "model_not_available", "unreachable")


@dataclass(frozen=True)
class RotationPolicy:
    members: tuple[str, ...]  # primary first
    on: tuple[str, ...] = ("usage_limit", "rate_limit", "authentication")
    cooldown_minutes: int = 60
    retry_seconds: int = 20


def load_rotations(document: dict[str, Json]) -> dict[str, RotationPolicy]:
    policies = {}
    for name, raw in mapping(document.get("rotation", {})).items():
        item = mapping(raw)
        if item.keys() - {"fallbacks", "on", "cooldown_minutes", "retry_seconds"}:
            raise ValueError("Unknown rotation fields")
        fallbacks = tuple(text(x, "fallback") for x in sequence(item.get("fallbacks", [])))
        if not fallbacks or name in fallbacks or len(set(fallbacks)) != len(fallbacks):
            raise ValueError(f"Rotation {name} needs distinct fallback profiles")
        default_on: list[Json] = list(RotationPolicy.on)
        on = tuple(text(x, "failure") for x in sequence(item.get("on", default_on)))
        if not on or set(on) - set(FAILURES):
            raise ValueError(f"Rotation {name}: unknown failure kinds")
        policy = RotationPolicy(
            (name, *fallbacks),
            on,
            integer(item.get("cooldown_minutes", 60), "cooldown_minutes"),
            integer(item.get("retry_seconds", 20), "retry_seconds"),
        )
        if not 1 <= policy.cooldown_minutes <= 10080 or not 5 <= policy.retry_seconds <= 3600:
            raise ValueError("Rotation cooldown must be 1..10080 minutes and retry 5..3600 seconds")
        policies[name] = policy
    return policies


class Cooldowns:
    """Profiles resting after a limit; a small JSON file next to the database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, dict[str, Json]]:
        try:
            raw = object_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: dict(mapping(value)) for key, value in raw.items()}

    def until(self, name: str) -> float:
        value = self.read().get(name, {}).get("until")
        return float(value) if isinstance(value, (int, float)) else 0.0

    def rest(self, name: str, until: float, reason: str) -> None:
        state: dict[str, Json] = dict(self.read())
        state[name] = {"until": until, "reason": reason}
        atomic_write(self.path, canonical(state))


class RotationHandler:
    def __init__(
        self,
        policy: RotationPolicy,
        members: dict[str, Handler],
        cooldowns: Cooldowns,
        clock: Callable[[], float],
    ) -> None:
        self.policy, self.members, self.cooldowns, self.clock = policy, members, cooldowns, clock
        primary = members[policy.members[0]].manifest
        # A native session belongs to one provider, so rotation never resumes one.
        capabilities = tuple(c for c in primary.capabilities if c != "resume")
        self.manifest = replace(
            primary,
            capabilities=capabilities,
            settings=canonical(
                {
                    "rotation": list(policy.members),
                    "members": {n: object_json(h.manifest.settings) for n, h in members.items()},
                }
            ),
        )

    def choose(self) -> tuple[str, float]:
        now = self.clock()
        for name in self.policy.members:
            if self.cooldowns.until(name) <= now:
                return name, 0.0
        return "", min(self.cooldowns.until(name) for name in self.policy.members)

    def prepare(self, packet: Packet) -> Launch:
        name, wait = self.choose()
        atomic_write(
            Path(packet.directory) / "rotation.json", canonical({"member": name, "wait": wait})
        )
        if not name:
            # Everyone is resting: a no-op process, then a wait until the first reset.
            return Launch((sys.executable, "-c", "pass"), packet.workspace)
        return self.members[name].prepare(packet)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        record = object_json((Path(packet.directory) / "rotation.json").read_text(encoding="utf-8"))
        name = text(record.get("member"), "member")
        now = self.clock()
        if not name:
            wait = record.get("wait")
            resume = float(wait) if isinstance(wait, (int, float)) else now + 60
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "waiting",
                "Every profile in the rotation is resting after a limit",
                revision,
                resume_at=min(max(resume, now + 5), now + 6.5 * 86400),
            )
        result = self.members[name].collect(packet, exit_code, revision)
        failure = object_json(result.data or "{}").get("failure")
        if result.outcome not in ("waiting", "blocked") or failure not in self.policy.on:
            return result
        rest = self.policy.cooldown_minutes * 60 if failure != "rate_limit" else 60
        until = max(result.resume_at or 0.0, now + rest)
        self.cooldowns.rest(name, until, str(failure))
        others = [m for m in self.policy.members if m != name and self.cooldowns.until(m) <= now]
        if others:
            return replace(
                result,
                outcome="waiting",
                reason=f"{name}: {str(failure).replace('_', ' ')}; next attempt uses {others[0]}",
                resume_at=now + self.policy.retry_seconds,
            )
        return replace(
            result,
            outcome="waiting",
            reason=f"{name}: {str(failure).replace('_', ' ')}; every profile is resting",
            resume_at=min(until, now + 6.5 * 86400),
        )


def apply_rotations(
    registry: Registry,
    policies: dict[str, RotationPolicy],
    cooldowns: Cooldowns,
    clock: Callable[[], float],
) -> None:
    for name, policy in policies.items():
        members = {member: registry.get(member) for member in policy.members}
        registry.replace(RotationHandler(policy, members, cooldowns, clock), name)
