"""Subscription quotas: read-only probes and the monitor that paces agents by them.

No probe starts a model turn. Claude reports its windows through the account usage
endpoint with the CLI's own sign-in; Codex through its native app-server. The
monitor reads each on the schedule `sdd_usage.quota` decides and applies the rests
it decides, so a spent subscription's profiles wait unstarted until the window
resets, and people see how much of every window remains.
"""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import mapping
from sdd_core.models import Json
from sdd_runtime.platform import NO_WINDOW
from sdd_runtime.rotation import Cooldowns
from sdd_runtime.versions import engine
from sdd_usage.quota import (
    REST_PREFIX,
    Quota,
    Rest,
    Window,
    claude_quota,
    codex_quota,
    next_reading,
    next_rest,
)

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROBE_ERRORS = (OSError, ValueError, KeyError, queue.Empty, urllib.error.URLError)


def claude_credentials() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / ".credentials.json"


def read_claude_quota(credentials: Path, now: float, timeout: float = 10) -> Quota:
    """Claude subscription windows. An API-key setup has none: it is billed per token."""
    try:
        oauth = mapping(json.loads(credentials.read_text(encoding="utf-8"))["claudeAiOauth"])
    except (OSError, ValueError, KeyError):
        return Quota("claude", "unavailable", checked_at=now, error="No subscription sign-in")
    plan = str(oauth.get("subscriptionType") or "")
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return Quota("claude", "unavailable", checked_at=now, plan=plan, error="No access token")
    if isinstance(expires, (int, float)) and expires / 1000 <= now:
        # The CLI refreshes its own sign-in on its next turn; the monitor never does.
        return Quota("claude", "unavailable", checked_at=now, plan=plan, error="Sign-in expired")
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": f"feature-factory-ai/{engine()}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return claude_quota(mapping(json.load(response)), now, plan)


def read_codex_buckets(argv: tuple[str, ...], timeout: float = 12) -> dict[str, Json]:
    """`account/rateLimits/read` through `codex app-server`; no thread or turn is created."""
    process = subprocess.Popen(
        [*argv, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=NO_WINDOW,
    )
    messages: queue.Queue[str | None] = queue.Queue(maxsize=256)
    assert process.stdout and process.stdin

    def read() -> None:
        assert process.stdout
        for line in process.stdout:
            try:
                messages.put_nowait(line)
            except queue.Full:
                break
        try:
            messages.put_nowait(None)
        except queue.Full:
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(value: dict[str, object]) -> None:
        assert process.stdin
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def response(identifier: int) -> dict[str, Json]:
        while True:
            line = messages.get(timeout=max(0, deadline - time.monotonic()))
            if line is None:
                raise ValueError("Account probe exited before replying")
            message = json.loads(line)
            if message.get("id") != identifier:
                continue
            if "error" in message:
                raise ValueError("Account endpoint unavailable; check CLI sign-in")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ValueError("Unrecognized account response")
            return result

    try:
        send(
            {
                "id": 0,
                "method": "initialize",
                "params": {"clientInfo": {"name": "feature_factory_ai", "version": engine()}},
            }
        )
        response(0)
        send({"method": "initialized"})
        send({"id": 1, "method": "account/rateLimits/read"})
        result = response(1)
        buckets = result.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            buckets = {"codex": result.get("rateLimits")}
        return buckets
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()


def read_codex_quota(argv: tuple[str, ...], now: float) -> Quota:
    return codex_quota(read_codex_buckets(argv), now)


def _window_view(window: Window) -> dict[str, Json]:
    return {
        "name": window.name,
        "used_percent": window.used_percent,
        "remaining_percent": window.remaining_percent,
        "resets_at": window.resets_at,
        "duration_minutes": window.minutes,
        "spent": window.spent,
    }


def overview(
    quotas: dict[str, Quota], adapters: dict[str, str], rests: dict[str, Rest], now: float
) -> dict[str, object]:
    """What people need to pace work: per subscription, how much of every window is
    left, when it resets and which profiles rest because of it."""
    subscriptions: list[Json] = []
    for adapter, quota in sorted(quotas.items()):
        profiles: list[Json] = []
        for name in sorted(p for p, a in adapters.items() if a == adapter):
            rest = rests.get(name)
            resting = rest is not None and rest.active(now)
            profiles.append(
                {
                    "name": name,
                    "resting_until": rest.until if resting and rest else None,
                    "reason": rest.reason if resting and rest else "",
                }
            )
        subscriptions.append(
            {
                "runner": quota.runner,
                "plan": quota.plan,
                "status": quota.status,
                "error": quota.error,
                "checked_at": quota.checked_at,
                "windows": [_window_view(w) for w in quota.windows],
                "profiles": profiles,
            }
        )
    checked = [q.checked_at for q in quotas.values()]
    return {
        "status": "available" if any(q.windows for q in quotas.values()) else "unavailable",
        "checked_at": max(checked) if checked else None,
        "subscriptions": subscriptions,
    }


class QuotaMonitor:
    """Reads due subscriptions and applies the rests their reports ask for.

    `adapters()` maps profile names to runner adapters; `probes` maps adapters to
    readers. Every decision (when to read, whether to rest) is `sdd_usage.quota`'s.
    """

    def __init__(
        self,
        cooldowns: Cooldowns,
        adapters: Callable[[], dict[str, str]],
        probes: dict[str, Callable[[float], Quota]],
    ) -> None:
        self.cooldowns, self.adapters, self.probes = cooldowns, adapters, probes
        self.lock = threading.Lock()
        self.quotas: dict[str, Quota] = {}
        self.due: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        # Refusal rests already answered with a fresh reading.
        self.answered: set[tuple[str, float]] = set()

    def refresh(self, now: float, force: bool = False) -> None:
        profiles = self.adapters()
        rests = self._rests()
        refused = self._refused(profiles, rests, now)
        for adapter in sorted(set(profiles.values()) & self.probes.keys()):
            if force or adapter in refused or now >= self.due.get(adapter, 0.0):
                self._read(adapter, now)
        for profile, adapter in profiles.items():
            quota = self.quotas.get(adapter)
            rest = None if quota is None else next_rest(quota, rests.get(profile), now)
            if rest is not None:
                self.cooldowns.rest(profile, rest.until, rest.reason)

    def _read(self, adapter: str, now: float) -> None:
        try:
            quota = self.probes[adapter](now)
        except PROBE_ERRORS as error:
            self.failures[adapter] = self.failures.get(adapter, 0) + 1
            message = f"{type(error).__name__}: {error}"[:200]
            last = self.quotas.get(adapter) or Quota(adapter, "unavailable", checked_at=now)
            quota = last.failed(message, now)
        else:
            self.failures[adapter] = 0
        self.due[adapter] = next_reading(quota, self.failures[adapter], now)
        with self.lock:
            self.quotas[adapter] = quota

    def _rests(self) -> dict[str, Rest]:
        rests: dict[str, Rest] = {}
        for profile, raw in self.cooldowns.read().items():
            until = raw.get("until")
            if isinstance(until, (int, float)) and not isinstance(until, bool):
                rests[profile] = Rest(float(until), str(raw.get("reason", "")))
        return rests

    def _refused(self, profiles: dict[str, str], rests: dict[str, Rest], now: float) -> set[str]:
        """Adapters a provider refused since the last reading: read them now."""
        fresh: set[str] = set()
        for profile, rest in rests.items():
            key = (profile, rest.until)
            refusal = rest.active(now) and not rest.reason.startswith(REST_PREFIX)
            if profile in profiles and refusal and key not in self.answered:
                self.answered.add(key)
                fresh.add(profiles[profile])
        return fresh

    def overview(self, now: float) -> dict[str, object]:
        with self.lock:
            quotas = dict(self.quotas)
        return overview(quotas, self.adapters(), self._rests(), now)
