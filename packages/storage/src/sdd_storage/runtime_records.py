"""SQLite runtime projections and atomic ownership arbitration."""

import sqlite3

from sdd_core.codec import canonical, digest, run_load
from sdd_core.models import Run
from sdd_core.records import EffectRecord
from sdd_core.storage_rules import (
    check_execution_binding,
    check_immutable,
    live_process,
    pin_changes,
    runnable,
)


class SQLiteRuntimeRecords:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def runs(self) -> tuple[Run, ...]:
        return tuple(
            run_load(str(row[0]))
            for row in self.db.execute("SELECT state FROM runs ORDER BY created,id")
        )

    def bind_handler(self, run_id: str, handler: str, manifest: str) -> None:
        row = self.db.execute(
            "SELECT manifest FROM bindings WHERE run=? AND handler=?", (run_id, handler)
        ).fetchone()
        live = any(
            live_process(str(kind), str(status))
            for kind, status in self.db.execute(
                "SELECT kind,status FROM effects WHERE run=?", (run_id,)
            )
        )
        if pin_changes(None if row is None else str(row[0]), manifest, live):
            self.db.execute(
                "INSERT INTO bindings VALUES(?,?,?) ON CONFLICT(run,handler) "
                "DO UPDATE SET manifest=excluded.manifest",
                (run_id, handler, manifest),
            )

    def context(self, run_id: str) -> str:
        row = self.db.execute("SELECT context FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row[0])

    def recent_results(self, run_id: str, limit: int) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("Positive result limit required")
        return tuple(
            str(row[0])
            for row in self.db.execute(
                "SELECT r.document FROM results r JOIN effects e ON e.id=r.attempt WHERE e.run=? ORDER BY e.rowid DESC LIMIT ?",
                (run_id, limit),
            )
        )

    def attempt_bases(self, run_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in self.db.execute(
                "SELECT json_extract(payload,'$.attempt.step'), "
                "json_extract(payload,'$.attempt.base_revision') FROM effects "
                "WHERE run=? AND json_extract(payload,'$.attempt') IS NOT NULL ORDER BY rowid",
                (run_id,),
            )
        )

    def step_results(self, run_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in self.db.execute(
                "SELECT json_extract(e.payload,'$.attempt.step'), r.document FROM results r "
                "JOIN effects e ON e.id=r.attempt WHERE e.run=? ORDER BY e.rowid DESC",
                (run_id,),
            )
        )

    def effect(self, attempt: str) -> EffectRecord:
        row = self.db.execute(
            "SELECT e.*,r.workspace,EXISTS(SELECT 1 FROM execution_requests x WHERE x.attempt=e.id) AS external "
            "FROM effects e JOIN runs r ON r.id=e.run WHERE e.id=?",
            (attempt,),
        ).fetchone()
        if row is None:
            raise KeyError(attempt)
        return EffectRecord(
            str(row["id"]),
            str(row["run"]),
            str(row["kind"]),
            str(row["status"]),
            str(row["workspace"]),
            row["pid"],
            row["created"],
            row["host_nonce"],
            row["receipt"],
            bool(row["external"]),
        )

    def effects(self, statuses: tuple[str, ...]) -> tuple[EffectRecord, ...]:
        placeholders = ",".join("?" for _ in statuses)
        ids = self.db.execute(
            f"SELECT id FROM effects WHERE status IN ({placeholders}) ORDER BY rowid", statuses
        ).fetchall()
        return tuple(self.effect(str(row[0])) for row in ids)

    def execution(self, attempt: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT backend,document FROM execution_requests WHERE attempt=?", (attempt,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def bind_execution(self, run_id: str, attempt: str, backend: str, document: str) -> None:
        check_execution_binding(
            self.effect(attempt), run_id, self.execution(attempt), (backend, document)
        )
        self.db.execute(
            "INSERT OR IGNORE INTO execution_requests VALUES(?,?,?)", (attempt, backend, document)
        )

    def runnable(self) -> tuple[str, ...]:
        rows = self.db.execute(
            "SELECT r.state,r.created,COALESCE((SELECT max(e.at) FROM events e "
            "WHERE e.run=r.id AND e.kind='dispatched'),r.created) FROM runs r"
        ).fetchall()
        runs = [
            (run_load(str(state)), float(created), float(last)) for state, created, last in rows
        ]
        status = {run.id: run.status for run, _, _ in runs}
        needs: dict[str, list[str]] = {}
        for run_id, prerequisite in self.db.execute("SELECT run,prerequisite FROM dependencies"):
            needs.setdefault(str(run_id), []).append(str(prerequisite))
        return runnable(
            (run, [status[key] for key in needs.get(run.id, ())], last, created)
            for run, created, last in runs
        )

    def queue_usage(self) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT COALESCE(sum(json_extract(state,'$.calls')),0),"
            "COALESCE(sum(json_extract(state,'$.planning_calls')),0) FROM runs"
        ).fetchone()
        return int(row[0]), int(row[1])

    def last_transition(self) -> float | None:
        value = self.db.execute("SELECT max(at) FROM events").fetchone()[0]
        return None if value is None else float(value)

    def portfolio(self, identifier: str) -> str | None:
        row = self.db.execute(
            "SELECT document FROM portfolios WHERE id=?", (identifier,)
        ).fetchone()
        return None if row is None else str(row[0])

    def bind_portfolio(self, identifier: str, document: str) -> None:
        check_immutable(
            self.portfolio(identifier), document, "Approved portfolio revision is immutable"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO portfolios VALUES(?,?,?)",
            (identifier, digest(document), document),
        )

    def lane(self, run_id: str) -> str | None:
        row = self.db.execute("SELECT document FROM lanes WHERE run=?", (run_id,)).fetchone()
        return None if row is None else str(row[0])

    def save_lane(self, run_id: str, document: str) -> None:
        self.db.execute(
            "INSERT INTO lanes VALUES(?,?) ON CONFLICT(run) DO UPDATE SET document=excluded.document",
            (run_id, document),
        )

    def set_policy(self, workspace: str, mandatory: tuple[str, ...]) -> None:
        self.db.execute(
            "INSERT INTO project_policies VALUES(?,?) ON CONFLICT(workspace) DO UPDATE SET mandatory=excluded.mandatory",
            (workspace, canonical(sorted(set(mandatory)))),
        )
