"""SQLite implementation of the application metadata port (schema version 3)."""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager

from sdd_core.catalog import AgentCall
from sdd_core.codec import object_json

type Transaction = Callable[[], AbstractContextManager[sqlite3.Connection]]


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
