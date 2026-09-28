"""UI-owned project metadata and measured usage; execution remains in the engine."""

import re
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import canonical, object_json, result_load, sequence, text
from sdd_core.models import Json
from sdd_core.ports import Conflict
from sdd_providers.catalog import adapters
from sdd_runtime.discovery import discover
from sdd_storage.store import Store


class WorkspaceCatalog:
    def __init__(self, store: Store) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ui_projects(id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ui_tasks(id TEXT PRIMARY KEY REFERENCES runs(id), document TEXT NOT NULL)"
            )
        self.discovery: list[dict[str, object]] = []
        self.subscription: dict[str, object] | None = None

    def projects(self) -> list[dict[str, Json]]:
        with self.store.transaction() as db:
            return [
                object_json(row[0])
                for row in db.execute("SELECT document FROM ui_projects ORDER BY id")
            ]

    def project(self, identifier: str) -> dict[str, Json]:
        return next((p for p in self.projects() if p["id"] == identifier), {})

    def save_project(self, doc: dict[str, Json]) -> dict[str, Json]:
        identifier, name = text(doc.get("id"), "id"), text(doc.get("name"), "name").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) or not name:
            raise ValueError("Project needs a valid ID and name")
        workspace = Path(text(doc.get("workspace"), "workspace")).resolve(strict=True)
        if not workspace.is_dir() or self.store.path.is_relative_to(workspace):
            raise ValueError("Project must be a folder outside the control database")
        language = text(doc.get("language", "ru"), "language")
        if language not in ("ru", "en"):
            raise ValueError("Unsupported response language")
        checks = [text(arg, "check argument") for arg in sequence(doc.get("checks", []))]
        if checks and (not Path(checks[0]).is_absolute() or not Path(checks[0]).is_file()):
            raise ValueError("Checks require an existing absolute executable")
        for existing in self.projects():
            if existing["id"] != identifier and Path(str(existing["workspace"])) == workspace:
                raise Conflict("This workspace is already registered")
            if existing["id"] == identifier and Path(str(existing["workspace"])) != workspace:
                raise Conflict("Project workspace is immutable; add another project")
        result: dict[str, Json] = {
            "id": identifier,
            "name": name,
            "workspace": str(workspace),
            "language": language,
            "checks": list[Json](checks),
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO ui_projects VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (identifier, canonical(result)),
            )
        return result

    def task_metadata(self) -> dict[str, dict[str, Json]]:
        with self.store.transaction() as db:
            return {
                str(row[0]): object_json(row[1])
                for row in db.execute("SELECT id,document FROM ui_tasks")
            }

    def save_task(self, identifier: str, metadata: dict[str, Json]) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO ui_tasks VALUES(?,?)", (identifier, canonical(metadata))
            )

    def discover(self) -> list[dict[str, object]]:
        self.discovery = [asdict(discover(adapter)) for adapter in adapters().values()]
        return self.discovery

    def usage(self) -> dict[str, object]:
        by_handler: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "active": 0, "unknown": 0}
        )
        daily: dict[str, int] = defaultdict(int)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT e.kind,e.status,e.payload,e.run,r.document FROM effects e LEFT JOIN results r ON r.attempt=e.id"
            ).fetchall()
            for row in rows:
                if row["kind"] != "agent":
                    continue
                payload = object_json(row["payload"])
                attempt = payload.get("attempt")
                if not isinstance(attempt, dict):
                    raise ValueError("Invalid effect attempt")
                step_id = text(attempt.get("step"), "step")
                run = self.store.load(db, str(row["run"]))
                definition = db.execute(
                    "SELECT document FROM definitions WHERE digest=?", (run.workflow_digest,)
                ).fetchone()
                from sdd_core.codec import workflow_load

                step = workflow_load(str(definition[0])).step(step_id)
                key = step.profile if step.profile != "default" else step.handler
                by_handler[key]["calls"] += 1
                by_handler[key]["active"] += int(
                    row["status"] in ("running", "pending", "uncertain")
                )
                if row["document"]:
                    usage = result_load(str(row["document"])).usage
                    by_handler[key]["unknown"] += int(
                        usage.input_tokens is None or usage.output_tokens is None
                    )
                    by_handler[key]["tokens"] += (usage.input_tokens or 0) + (
                        usage.output_tokens or 0
                    )
            for row in db.execute(
                "SELECT date(at,'unixepoch') AS day,count(*) AS n FROM events WHERE kind='dispatched' GROUP BY day ORDER BY day DESC LIMIT 14"
            ):
                daily[str(row["day"])] = int(row["n"])
        return {
            "by_handler": dict(by_handler),
            "daily_dispatches": dict(sorted(daily.items())),
            "subscription": self.subscription,
        }
