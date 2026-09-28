"""Flight records: everything needed to explain what happened to one task.

A record joins the engine journal (transitions), the flight log (operator
actions, queue incidents) and the tails of the latest attempt's logs. It is a
read-only view; `briefing` condenses it into guidance a recovery step can read.
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sdd_core.models import Json, Run
from sdd_runtime.engine import Engine
from sdd_runtime.files import attempt_folder

from sdd_factory.journal import FlightLog

TAIL_CHARS = 1500
FILES = ("stderr.log", "stdout.log", "exit.json", "rotation.json")


@dataclass(frozen=True)
class FlightRecord:
    generated_at: float
    run: Run
    events: list[dict[str, object]]
    log: list[dict[str, Json]]
    attempt: str | None
    files: dict[str, str]

    def document(self) -> dict[str, object]:
        return asdict(self)

    def briefing(self) -> str:
        """A short incident summary for the next recovery step, newest facts last."""
        lines = [f"Incident summary (status {self.run.status}, step {self.run.step}):"]
        if self.run.reason:
            lines.append("Reason: " + self.run.reason)
        for event in self.events[-8:]:
            detail = str(event.get("detail") or "")[:200]
            lines.append(f"- {event['kind']}" + (f": {detail}" if detail else ""))
        stderr = self.files.get("stderr.log", "").strip()
        if stderr:
            lines.append("Last stderr lines:")
            lines.extend(stderr.splitlines()[-8:])
        return "\n".join(lines)[:3000]


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - TAIL_CHARS))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def record(engine: Engine, log: FlightLog, run_id: str) -> FlightRecord:
    run = engine.store.get(run_id)
    with engine.store.unit() as db:
        root = Path(db.location(run_id)[0])
    attempt = run.active.id if run.active else run.previous_attempt
    files: dict[str, str] = {}
    if attempt:
        folder = attempt_folder(root, run_id, attempt)
        files = {name: _tail(folder / name) for name in FILES if (folder / name).is_file()}
    return FlightRecord(
        time.time(),
        run,
        engine.store.history(run_id, limit=1000)[-60:],
        log.read(limit=100, run=run_id),
        attempt,
        files,
    )


LIVE_LINES = 40
LIVE_BYTES = 64 * 1024


def _event(line: str) -> str:
    """One readable line for a streamed CLI event; plain text passes through."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        item = json.loads(stripped)
    except ValueError:
        return stripped
    if not isinstance(item, dict):
        return stripped
    kind = str(item.get("type", ""))
    raw = item.get("item")
    inner: dict[str, object] = raw if isinstance(raw, dict) else {}
    detail = (
        inner.get("text")
        or inner.get("command")
        or item.get("text")
        or item.get("message")
        or item.get("result")
        or ""
    )
    label = "/".join(x for x in (kind, str(inner.get("type", ""))) if x)
    return f"[{label}] {str(detail)[:400]}".strip() if label else stripped[:400]


def _recent(path: Path) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - LIVE_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [_event(line) for line in text.splitlines()[1:] or text.splitlines()]
    return [line for line in lines if line][-LIVE_LINES:]


def live(engine: Engine, run_id: str, hosted: bool) -> dict[str, object]:
    """What the current attempt is doing now, read from its attempt folder.

    `hosted` says whether this coordinator owns a live host for the attempt.
    """
    run = engine.store.get(run_id)
    now = time.time()
    if run.active is None:
        return {"active": False, "now": now}
    with engine.store.unit() as db:
        root = Path(db.location(run_id)[0])
    folder = attempt_folder(root, run_id, run.active.id)
    streams: dict[str, object] = {}
    last_output = None
    for name in ("stdout.log", "stderr.log"):
        path = folder / name
        if path.is_file():
            stat = path.stat()
            last_output = max(last_output or 0.0, stat.st_mtime)
            streams[name] = {"bytes": stat.st_size, "lines": _recent(path)}
    return {
        "active": True,
        "now": now,
        "attempt": run.active.id,
        "step": run.active.step,
        "started": run.active.started,
        "deadline": run.active.deadline,
        "hosted": hosted,
        "last_output": last_output,
        "streams": streams,
    }
