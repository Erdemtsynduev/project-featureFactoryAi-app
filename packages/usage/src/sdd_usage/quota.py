"""Subscription windows and when a subscription may take work. Pure: no IO, no clock.

A subscription (Claude Pro/Max, a ChatGPT plan for Codex) is not billed per call:
it grants usage in rolling windows (five hours, a week) and refuses work once a
window is spent. The engine therefore paces agents by these windows, not by
counting calls: a profile whose window is spent rests until the window resets, and
every other profile keeps working. This module holds the model and every decision;
the probes that fetch the provider's report and the store of rests live outside.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sdd_core.codec import number
from sdd_core.models import Json

type QuotaStatus = Literal["available", "unavailable"]

# Spent means at or above this share of a window: providers refuse at 100.
SPENT_PERCENT = 100.0
# A window this full is read more often, so its end is seen promptly.
CLOSE_PERCENT = 90.0
# Reading schedule: providers' usage endpoints have small budgets of their own.
POLL_SECONDS = 15 * 60
CLOSE_POLL_SECONDS = 5 * 60
MAX_BACKOFF_SECONDS = 60 * 60
# Rests decided from a subscription report carry this prefix; only those are lifted
# by a later report. A rest set by a refusal stays: it may name a limit the report omits.
REST_PREFIX = "subscription: "
REOPENED = "subscription window reopened"


@dataclass(frozen=True)
class Window:
    """One usage window: its name ("session", "weekly"), share used and reset moment."""

    name: str
    used_percent: float
    resets_at: float | None = None
    minutes: int | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @property
    def spent(self) -> bool:
        return self.used_percent >= SPENT_PERCENT


@dataclass(frozen=True)
class Quota:
    """What a runner's subscription reported at `checked_at`."""

    runner: str
    status: QuotaStatus
    windows: tuple[Window, ...] = ()
    checked_at: float = 0.0
    plan: str = ""
    error: str = ""

    @property
    def current(self) -> bool:
        """A usable reading: the provider answered and nothing failed since."""
        return self.status == "available" and not self.error

    def rest_until(self, now: float) -> float | None:
        """Until when the subscription can take no work, or None when it can now.

        Every spent window must reset first, so the latest reset wins. A spent window
        without a known reset gives no moment to wait for.
        """
        resets = [w.resets_at for w in self.windows if w.spent]
        known = [moment for moment in resets if moment is not None and moment > now]
        return max(known) if known else None

    def describe(self) -> str:
        spent = ", ".join(f"{w.name} {w.used_percent:.0f}%" for w in self.windows if w.spent)
        return f"{self.runner} subscription spent ({spent})" if spent else ""

    def failed(self, error: str, now: float) -> "Quota":
        """The last reading kept, marked with why reading it again failed."""
        if self.status == "unavailable" and not self.windows:
            return Quota(self.runner, "unavailable", checked_at=now, plan=self.plan, error=error)
        return Quota(self.runner, self.status, self.windows, self.checked_at, self.plan, error)


@dataclass(frozen=True)
class Rest:
    """A profile resting until `until`; `until` in the past means it may work."""

    until: float
    reason: str

    def active(self, now: float) -> bool:
        return self.until > now


def next_rest(quota: Quota, current: Rest | None, now: float) -> Rest | None:
    """The rest a subscription report asks for, or None to leave the profile as it is.

    A spent subscription rests the profile until the window resets. A report with
    room lifts only a rest an earlier report set.
    """
    if not quota.current:
        return None
    until = quota.rest_until(now)
    if until is not None:
        wanted = Rest(until, REST_PREFIX + quota.describe())
        return None if current == wanted else wanted
    if current is not None and current.active(now) and current.reason.startswith(REST_PREFIX):
        return Rest(0.0, REOPENED)
    return None


def next_reading(quota: Quota | None, failures: int, now: float) -> float:
    """When to read a subscription again: rarely, sooner near a limit, backing off on errors."""
    if failures:
        doubled = CLOSE_POLL_SECONDS << min(failures, 10)  # doubles per failure
        return now + min(MAX_BACKOFF_SECONDS, doubled)
    close = quota is not None and any(w.used_percent >= CLOSE_PERCENT for w in quota.windows)
    return now + (CLOSE_POLL_SECONDS if close else POLL_SECONDS)


# Readers of the providers' reports -------------------------------------------------


def _moment(value: Json) -> float | None:
    """An epoch number or an ISO-8601 timestamp with its offset."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return number(value)


def _percent(value: Json) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


CLAUDE_WINDOWS = (
    ("five_hour", "session", 300),
    ("seven_day", "weekly", 10080),
    ("seven_day_opus", "weekly opus", 10080),
    ("seven_day_sonnet", "weekly sonnet", 10080),
)
CODEX_WINDOW_NAMES = {300: "session", 10080: "weekly"}


def claude_quota(document: dict[str, Json], checked_at: float, plan: str = "") -> Quota:
    """The Claude account usage report: `five_hour`, `seven_day` and model-scoped weeks."""
    windows: list[Window] = []
    for key, name, minutes in CLAUDE_WINDOWS:
        raw = document.get(key)
        used = _percent(raw.get("utilization")) if isinstance(raw, dict) else None
        if isinstance(raw, dict) and used is not None:
            windows.append(Window(name, used, _moment(raw.get("resets_at")), minutes))
    status: QuotaStatus = "available" if windows else "unavailable"
    return Quota("claude", status, tuple(windows), checked_at, plan)


def codex_quota(buckets: dict[str, Json], checked_at: float, plan: str = "") -> Quota:
    """Codex app-server `rateLimitsByLimitId`: primary and secondary windows per bucket."""
    windows: list[Window] = []
    for bucket, raw in buckets.items():
        for kind in ("primary", "secondary"):
            value = raw.get(kind) if isinstance(raw, dict) else None
            used = _percent(value.get("usedPercent")) if isinstance(value, dict) else None
            if not isinstance(value, dict) or used is None:
                continue
            span = value.get("windowDurationMins")
            minutes = span if isinstance(span, int) and not isinstance(span, bool) else None
            name = CODEX_WINDOW_NAMES.get(minutes or 0, kind)
            label = name if bucket == "codex" else f"{bucket} {name}"
            windows.append(Window(label, used, _moment(value.get("resetsAt")), minutes))
    status: QuotaStatus = "available" if windows else "unavailable"
    return Quota("codex", status, tuple(windows), checked_at, plan)
