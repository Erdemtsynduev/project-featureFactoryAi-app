"""SQLite implementation of the application metadata port and the tracker outbox."""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager

from sdd_core.catalog import AgentCall, OutboxEntry
from sdd_core.codec import object_json

type Transaction = Callable[[], AbstractContextManager[sqlite3.Connection]]

# The catalog's tables, created by the store's schema migrations in their order.
# Application metadata (projects, plans, task labels, preferences); earlier releases
# created these from the UI, and IF NOT EXISTS adopts them unchanged.
METADATA_TABLES = """
CREATE TABLE IF NOT EXISTS ui_projects(id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_tasks(id TEXT PRIMARY KEY REFERENCES runs(id), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_plans(project TEXT NOT NULL, id TEXT NOT NULL,
    document TEXT NOT NULL, PRIMARY KEY(project, id));
CREATE TABLE IF NOT EXISTS ui_state(key TEXT PRIMARY KEY, document TEXT NOT NULL);
"""
# A feature's specification and ticket breakdown, kept by the factory.
ARTIFACT_TABLES = """
CREATE TABLE IF NOT EXISTS ui_artifacts(run TEXT NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(run, kind));
"""
# Publications to a project's tracker, recorded before delivery (an outbox).
OUTBOX_TABLES = """
CREATE TABLE IF NOT EXISTS tracker_outbox(id TEXT PRIMARY KEY, project TEXT NOT NULL,
    run TEXT NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL, next_at REAL NOT NULL, error TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tracker_outbox_item ON tracker_outbox(run, kind);
CREATE INDEX IF NOT EXISTS tracker_outbox_pending ON tracker_outbox(project, status);
"""
# Catalog rows that belong to one run: they leave with it.
RUN_ROWS = (("ui_tasks", "id"), ("ui_artifacts", "run"))


def forget_runs(db: sqlite3.Connection, identifiers: list[tuple[str]]) -> None:
    """Remove the catalog rows of discarded runs, in the store's transaction."""
    for table, column in RUN_ROWS:
        db.executemany(f"DELETE FROM {table} WHERE {column}=?", identifiers)


class SQLiteCatalog:
    """Each call is one short transaction; documents are stored opaquely."""

    def __init__(self, transaction: Transaction) -> None:
        self.transaction = transaction

    def projects(self) -> tuple[str, ...]:
        with self.transaction() as db:
            return tuple(
                str(row[0]) for row in db.execute("SELECT document FROM ui_projects ORDER BY id")
            )

    def save_project(self, identifier: str, document: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO ui_projects VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (identifier, document),
            )

    def plans(self) -> tuple[tuple[str, str], ...]:
        with self.transaction() as db:
            return tuple(
                (str(row[0]), str(row[1]))
                for row in db.execute("SELECT project, document FROM ui_plans ORDER BY project, id")
            )

    def save_plans(self, project: str, plans: tuple[tuple[str, str], ...]) -> None:
        with self.transaction() as db:
            db.executemany(
                "INSERT INTO ui_plans VALUES(?,?,?) ON CONFLICT(project, id) "
                "DO UPDATE SET document=excluded.document",
                [(project, identifier, document) for identifier, document in plans],
            )

    def tasks(self) -> tuple[tuple[str, str], ...]:
        with self.transaction() as db:
            return tuple(
                (str(row[0]), str(row[1])) for row in db.execute("SELECT id,document FROM ui_tasks")
            )

    def save_task(self, identifier: str, document: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO ui_tasks VALUES(?,?)", (identifier, document))

    def update_task(self, identifier: str, document: str) -> None:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE ui_tasks SET document=? WHERE id=?", (document, identifier)
            ).rowcount
            if changed != 1:
                raise KeyError(identifier)

    def artifacts(self, run: str) -> tuple[tuple[str, str], ...]:
        with self.transaction() as db:
            return tuple(
                (str(row[0]), str(row[1]))
                for row in db.execute(
                    "SELECT kind, document FROM ui_artifacts WHERE run=? ORDER BY kind", (run,)
                )
            )

    def save_artifact(self, run: str, kind: str, document: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO ui_artifacts VALUES(?,?,?) ON CONFLICT(run, kind) "
                "DO UPDATE SET document=excluded.document",
                (run, kind, document),
            )

    def preference(self, key: str) -> str | None:
        with self.transaction() as db:
            row = db.execute("SELECT document FROM ui_state WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def save_preference(self, key: str, document: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO ui_state VALUES(?,?) ON CONFLICT(key) "
                "DO UPDATE SET document=excluded.document",
                (key, document),
            )

    def agent_calls(self) -> tuple[AgentCall, ...]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT e.run, json_extract(r.state,'$.workflow_digest') AS digest, e.payload, "
                "e.status, x.document FROM effects e JOIN runs r ON r.id=e.run "
                "LEFT JOIN results x ON x.attempt=e.id WHERE e.kind='agent' ORDER BY e.rowid"
            ).fetchall()
        calls = []
        for row in rows:
            attempt = object_json(str(row[2])).get("attempt")
            if not isinstance(attempt, dict) or not isinstance(attempt.get("step"), str):
                raise ValueError("Invalid effect attempt")
            started = attempt.get("started")
            calls.append(
                AgentCall(
                    str(row[0]),
                    str(row[1]),
                    str(attempt["step"]),
                    float(started)
                    if isinstance(started, (int, float)) and not isinstance(started, bool)
                    else None,
                    str(row[3]),
                    None if row[4] is None else str(row[4]),
                )
            )
        return tuple(calls)

    def daily_dispatches(self, days: int) -> tuple[tuple[str, int], ...]:
        with self.transaction() as db:
            return tuple(
                (str(row[0]), int(row[1]))
                for row in db.execute(
                    "SELECT date(at,'unixepoch') AS day, count(*) FROM events "
                    "WHERE kind='dispatched' GROUP BY day ORDER BY day DESC LIMIT ?",
                    (days,),
                )
            )

    # Tracker outbox ------------------------------------------------------------

    def record_update(self, entry: OutboxEntry) -> bool:
        with self.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO tracker_outbox VALUES(?,?,?,?,?,?,?,?,?,?)",
                _outbox_row(entry),
            ).rowcount
        return inserted == 1

    def last_update(self, run: str, kind: str) -> OutboxEntry | None:
        with self.transaction() as db:
            row = db.execute(
                f"SELECT {OUTBOX_COLUMNS} FROM tracker_outbox WHERE run=? AND kind=? "
                "ORDER BY rowid DESC LIMIT 1",
                (run, kind),
            ).fetchone()
        return None if row is None else _outbox_entry(row)

    def pending_updates(self, project: str) -> tuple[OutboxEntry, ...]:
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT {OUTBOX_COLUMNS} FROM tracker_outbox "
                "WHERE project=? AND status='pending' ORDER BY rowid",
                (project,),
            ).fetchall()
        return tuple(_outbox_entry(row) for row in rows)

    def settle_update(self, entry: OutboxEntry) -> None:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE tracker_outbox SET status=?,attempts=?,next_at=?,error=?,receipt=? "
                "WHERE id=?",
                (entry.status, entry.attempts, entry.next_at, entry.error, entry.receipt, entry.id),
            ).rowcount
        if changed != 1:
            raise KeyError(entry.id)

    def updates(self, project: str, limit: int) -> tuple[OutboxEntry, ...]:
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT {OUTBOX_COLUMNS} FROM tracker_outbox WHERE project=? "
                "ORDER BY rowid DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        return tuple(_outbox_entry(row) for row in rows)


OUTBOX_COLUMNS = "id,project,run,kind,document,status,attempts,next_at,error,receipt"


def _outbox_row(entry: OutboxEntry) -> tuple[object, ...]:
    return (
        entry.id,
        entry.project,
        entry.run,
        entry.kind,
        entry.document,
        entry.status,
        entry.attempts,
        entry.next_at,
        entry.error,
        entry.receipt,
    )


def _outbox_entry(row: sqlite3.Row) -> OutboxEntry:
    return OutboxEntry(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        int(row[6]),
        float(row[7]),
        str(row[8]),
        str(row[9]),
    )
