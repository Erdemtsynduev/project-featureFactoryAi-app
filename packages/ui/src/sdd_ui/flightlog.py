"""Append-only flight log of operator actions and queue incidents.

The engine journal already records every run transition. This log adds what the
journal cannot see: which operator action was requested and how it ended, queue
stops, contained failures and automatic revivals. Together they let a person or a
recovery step reconstruct an incident afterwards. Lines are JSON, bounded in
size, rotated by count, and never contain tokens or answer drafts.
"""

import json
import threading
import time
from collections import deque
from pathlib import Path

from sdd_core.models import Json

MAX_BYTES = 4 * 1024 * 1024
BACKUPS = 3
FIELD_CHARS = 2000


def _bounded(value: Json) -> Json:
    if isinstance(value, str) and len(value) > FIELD_CHARS:
        return value[:FIELD_CHARS] + " …"
    if isinstance(value, dict):
        return {key: _bounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded(item) for item in value[:50]]
    return value


class FlightLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, kind: str, level: str = "info", run: str = "", **fields: Json) -> None:
        entry: dict[str, Json] = {"at": round(time.time(), 3), "kind": kind, "level": level}
        if run:
            entry["run"] = run
        entry.update({key: _bounded(value) for key, value in fields.items()})
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._rotate()
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError:
                pass  # Diagnostics never stop the queue.

    def _rotate(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < MAX_BYTES:
            return
        for index in range(BACKUPS, 0, -1):
            source = self.path.with_suffix(f".{index - 1}.jsonl") if index > 1 else self.path
            target = self.path.with_suffix(f".{index}.jsonl")
            if source.exists():
                source.replace(target)

    def read(self, limit: int = 200, run: str = "", level: str = "") -> list[dict[str, Json]]:
        """Newest entries last, optionally for one run or at least one level."""
        wanted = {"error": ("error",), "warning": ("warning", "error")}.get(level)
        lines: deque[dict[str, Json]] = deque(maxlen=max(1, min(limit, 2000)))
        paths = [self.path.with_suffix(f".{i}.jsonl") for i in range(BACKUPS, 0, -1)]
        for path in [*paths, self.path]:
            try:
                handle = path.open(encoding="utf-8")
            except OSError:
                continue
            with handle:
                for raw in handle:
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if run and entry.get("run") != run:
                        continue
                    if wanted and entry.get("level") not in wanted:
                        continue
                    lines.append(entry)
        return list(lines)
