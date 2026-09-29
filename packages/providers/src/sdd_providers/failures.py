"""Classify agent CLI failures so the engine can wait for a reset or rotate agents.

Only recognised messages are classified; anything else stays `other` and keeps
blocking with the original diagnosis. Reset times are taken from the message
when it states one; otherwise callers apply a configured cooldown.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

AUTHENTICATION = re.compile(
    r"not (?:authenticated|logged in|signed in)|please (?:log|sign) in|login required|"
    r"authentication (?:required|failed)|unauthori[sz]ed|invalid api key|\b401\b",
    re.IGNORECASE,
)
USAGE_LIMIT = re.compile(
    r"usage limit|hit your (?:usage )?limit|limit reached|out of (?:credits|usage)|"
    r"quota (?:exceeded|exhausted)|exceeded your (?:current )?quota|weekly limit|"
    r"insufficient (?:credits|balance)",
    re.IGNORECASE,
)
RATE_LIMIT = re.compile(
    r"rate.?limit|too many requests|\b429\b|overloaded|capacity|slow down", re.IGNORECASE
)
UNAVAILABLE = re.compile(
    r"model .{0,40}(?:not (?:available|supported|found)|does not exist)|unknown model",
    re.IGNORECASE,
)
UNREACHABLE = re.compile(
    r"ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|getaddrinfo|network error|connection "
    r"(?:refused|reset)|bad gateway|service unavailable|gateway timeout|\b50[234]\b",
    re.IGNORECASE,
)
EPOCH = re.compile(r"\|(\d{10})(?:\d{3})?\b")
RELATIVE = re.compile(
    r"(?:try again|retry|resets?|available again)\s+in\s+"
    # Longest unit spellings first, so "hours" is not read as "h" + leftover text.
    r"(?:(\d+)\s*(?:days?|d)\b)?\s*(?:(\d+)\s*(?:hours?|hrs?|h)\b)?\s*"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\b)?",
    re.IGNORECASE,
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_CUE = r"(?:try again|resets?|available again)\s+(?:at|on)?\s*"
# No trailing \b: "11:41 PM." ends in a dot, which a word boundary would refuse.
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m(?![a-z])"
# "try again at Oct 4th, 2026 11:41 PM" (Codex weekly limit), local wall time.
DATED = re.compile(
    _CUE + r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?,?\s*(?:at\s+)?" + _CLOCK,
    re.IGNORECASE,
)
# "resets 3pm", "try again at 11:41 PM": the next such local time.
CLOCK = re.compile(_CUE + _CLOCK, re.IGNORECASE)


@dataclass(frozen=True)
class Failure:
    # usage_limit | rate_limit | authentication | model_not_available | unreachable | other
    kind: str
    reset_at: float | None = None
    excerpt: str = ""


def classify(message: str, now: float) -> Failure:
    text = message[-20000:]
    excerpt = next((line.strip()[:300] for line in reversed(text.splitlines()) if line.strip()), "")
    if AUTHENTICATION.search(text):
        return Failure("authentication", None, excerpt)
    if UNAVAILABLE.search(text):
        return Failure("model_not_available", None, excerpt)
    if USAGE_LIMIT.search(text):
        return Failure("usage_limit", reset_time(text, now), excerpt)
    if RATE_LIMIT.search(text):
        return Failure("rate_limit", reset_time(text, now), excerpt)
    if UNREACHABLE.search(text):
        return Failure("unreachable", None, excerpt)
    return Failure("other", None, excerpt)


def reset_time(text: str, now: float) -> float | None:
    """An explicit reset moment from the message, bounded to one week."""
    epoch = EPOCH.search(text)
    if epoch:
        value = float(epoch.group(1))
        return value if now < value <= now + 7 * 86400 else None
    relative = RELATIVE.search(text)
    if relative and any(relative.groups()):
        days, hours, minutes = (int(x or 0) for x in relative.groups())
        seconds = days * 86400 + hours * 3600 + minutes * 60
        return now + seconds if 0 < seconds <= 7 * 86400 else None
    moment = _dated(text, now) or _clock(text, now)
    return moment if moment is not None and now < moment <= now + 7 * 86400 else None


def _hour(hour: str, meridiem: str) -> int:
    value = int(hour) % 12
    return value + 12 if meridiem.lower() == "p" else value


def _dated(text: str, now: float) -> float | None:
    found = DATED.search(text)
    if not found:
        return None
    month_name, day, year, hour, minute, meridiem = found.groups()
    month = month_name[:3].lower()
    if month not in _MONTHS or not 1 <= int(hour) <= 12:
        return None
    current = datetime.fromtimestamp(now)
    try:
        moment = datetime(
            int(year) if year else current.year,
            _MONTHS.index(month) + 1,
            int(day),
            _hour(hour, meridiem),
            int(minute or 0),
        )
    except ValueError:
        return None
    if not year and moment < current:
        moment = moment.replace(year=current.year + 1)
    return moment.timestamp()


def _clock(text: str, now: float) -> float | None:
    found = CLOCK.search(text)
    if not found or not 1 <= int(found.group(1)) <= 12:
        return None
    current = datetime.fromtimestamp(now)
    moment = current.replace(
        hour=_hour(found.group(1), found.group(3)),
        minute=int(found.group(2) or 0),
        second=0,
        microsecond=0,
    )
    if moment <= current:
        moment += timedelta(days=1)
    return moment.timestamp()
