"""Agent failures become waits; rotation rests exhausted profiles and switches."""

import json
import sys
from pathlib import Path

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Attempt, Result, Step
from sdd_core.sdk import Launch, Manifest, Packet
from sdd_providers.failures import classify
from sdd_providers.handlers import CliHandler
from sdd_runtime.rotation import Cooldowns, RotationHandler, RotationPolicy, load_rotations

NOW = 1_800_000_000.0


def test_classifier_reads_kinds_and_reset_times():
    usage = classify(f"Claude AI usage limit reached|{int(NOW) + 3600}", NOW)
    assert usage.kind == "usage_limit" and usage.reset_at == NOW + 3600
    later = classify("You've hit your usage limit. Try again in 2 hours 5 minutes.", NOW)
    assert later.kind == "usage_limit" and later.reset_at == NOW + 7500
    assert classify("429 Too Many Requests", NOW).kind == "rate_limit"
    assert classify("Error: Not logged in. Please log in", NOW).kind == "authentication"
    assert classify("connect ECONNREFUSED 127.0.0.1", NOW).kind == "unreachable"
    assert classify("TypeError: boom", NOW).kind == "other"
    assert classify("usage limit|99999999999", NOW).reset_at is None  # beyond a week


def packet(tmp_path: Path) -> Packet:
    folder = tmp_path / "attempt"
    folder.mkdir(exist_ok=True)
    step = Step("plan", "agent", "codex", "Plan", (("done", "next"),))
    return Packet(
        "r", Attempt("a1", "plan", 1, 0, NOW + 900, "rev"), step, str(tmp_path), str(folder), "ctx"
    )


def test_cli_limit_becomes_a_bounded_wait(tmp_path):
    handler = CliHandler("codex", sys.executable, "gpt")
    item = packet(tmp_path)
    Path(item.directory, "stderr.log").write_text(
        "ERROR: You've hit your usage limit. Try again in 3 hours.", encoding="utf-8"
    )
    result = handler.collect(item, 1, "rev")
    assert result.outcome == "waiting" and "usage limit" in result.reason
    assert json.loads(result.data)["failure"] == "usage_limit"
    Path(item.directory, "stderr.log").write_text("Segmentation fault", encoding="utf-8")
    assert handler.collect(item, 1, "rev").outcome == "blocked"


class Fake:
    def __init__(self, name: str, outcome: str = "done", failure: str | None = None) -> None:
        self.name, self.outcome, self.failure = name, outcome, failure
        self.manifest = Manifest(name, "1", capabilities=("process", "agent", "resume"))
        self.prepared = 0

    def prepare(self, packet: Packet) -> Launch:
        self.prepared += 1
        return Launch((sys.executable, "-c", "pass"), packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        data = canonical({"failure": self.failure}) if self.failure else "{}"
        resume = NOW + 30 if self.outcome == "waiting" else None
        return Result(
            packet.attempt.id, 1, self.outcome, self.name, revision, resume_at=resume, data=data
        )


def test_rotation_rests_the_exhausted_profile_and_switches(tmp_path):
    clock = [NOW]
    codex = Fake("codex", "waiting", "usage_limit")
    claude = Fake("claude")
    cooldowns = Cooldowns(tmp_path / "cooldowns.json")
    policy = RotationPolicy(("codex", "claude"), cooldown_minutes=60, retry_seconds=20)
    rotation = RotationHandler(
        policy, {"codex": codex, "claude": claude}, cooldowns, lambda: clock[0]
    )
    assert "resume" not in rotation.manifest.capabilities
    item = packet(tmp_path)
    rotation.prepare(item)
    first = rotation.collect(item, 1, "rev")
    assert first.outcome == "waiting" and first.resume_at == NOW + 20 and "claude" in first.reason
    assert cooldowns.until("codex") == NOW + 3600
    rotation.prepare(item)
    assert claude.prepared == 1 and rotation.collect(item, 0, "rev").reason == "claude"
    clock[0] = NOW + 3601  # cooldown over: the primary is preferred again
    rotation.prepare(item)
    assert codex.prepared == 2


def test_everyone_resting_waits_for_the_first_reset(tmp_path):
    cooldowns = Cooldowns(tmp_path / "cooldowns.json")
    cooldowns.rest("codex", NOW + 500, "usage_limit")
    cooldowns.rest("claude", NOW + 200, "usage_limit")
    rotation = RotationHandler(
        RotationPolicy(("codex", "claude")),
        {"codex": Fake("codex"), "claude": Fake("claude")},
        cooldowns,
        lambda: NOW,
    )
    item = packet(tmp_path)
    assert rotation.prepare(item).argv[-1] == "pass"
    result = rotation.collect(item, 0, "rev")
    assert result.outcome == "waiting" and result.resume_at == NOW + 200


def test_rotation_configuration_is_validated():
    policies = load_rotations(
        {"rotation": {"codex": {"fallbacks": ["claude"], "cooldown_minutes": 30}}}
    )
    assert (
        policies["codex"].members == ("codex", "claude")
        and policies["codex"].cooldown_minutes == 30
    )
    for bad in (
        {"codex": {"fallbacks": []}},
        {"codex": {"fallbacks": ["codex"]}},
        {"codex": {"fallbacks": ["claude"], "on": ["weather"]}},
        {"codex": {"fallbacks": ["claude"], "retry_seconds": 1}},
    ):
        with pytest.raises(ValueError):
            load_rotations({"rotation": bad})
