"""SQLite runtime projections and atomic ownership arbitration."""

import sqlite3

from sdd_core.codec import canonical, digest, run_load
from sdd_core.models import Run
from sdd_core.ports import Conflict
from sdd_core.runtime_ports import EffectRecord


class SQLiteRuntimeRecords:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def runs(self) -> tuple[Run, ...]:
        return tuple(
            run_load(str(row[0]))
            for row in self.db.execute("SELECT state FROM runs ORDER BY created,id")
        )

    def bind_handler(self, run_id: str, handler: str, manifest: str) -> None:
        old = self.db.execute(
            "SELECT manifest FROM bindings WHERE run=? AND handler=?", (run_id, handler)
        ).fetchone()
        if old and old[0] != manifest:
            raise Conflict("Pinned handler settings or version changed")
        self.db.execute("INSERT OR IGNORE INTO bindings VALUES(?,?,?)", (run_id, handler, manifest))

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

    def claim_host(self, attempt: str, packet: str, nonce: str) -> None:
        record = self.effect(attempt)
        if (
            record.external
            or record.status != "pending"
            or record.host_nonce is not None
            or record.pid is not None
        ):
            raise Conflict("Attempt already claimed")
        self.db.execute(
            "UPDATE effects SET packet=?,host_nonce=? WHERE id=?", (packet, nonce, attempt)
        )

    def host_started(self, attempt: str, nonce: str, pid: int, created: float) -> None:
        cursor = self.db.execute(
            "UPDATE effects SET status='running',pid=?,created=? WHERE id=? AND status='pending' AND host_nonce=? AND pid IS NULL",
            (pid, created, attempt, nonce),
        )
        if cursor.rowcount != 1:
            raise Conflict("Dispatch ownership changed before launch")

    def execution(self, attempt: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT backend,document FROM execution_requests WHERE attempt=?", (attempt,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def bind_execution(self, run_id: str, attempt: str, backend: str, document: str) -> None:
        record = self.effect(attempt)
        if record.run_id != run_id or record.status not in ("pending", "running"):
            raise Conflict("Attempt is not available for execution")
        old = self.execution(attempt)
        if old is not None and old != (backend, document):
            raise Conflict("Execution request is immutable")
        if record.host_nonce is not None or record.pid is not None:
            raise Conflict("Attempt already belongs to a local host")
        self.db.execute(
            "INSERT OR IGNORE INTO execution_requests VALUES(?,?,?)", (attempt, backend, document)
        )

    def runnable(self) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in self.db.execute(
                "SELECT r.id FROM runs r WHERE json_extract(state,'$.paused')=0 "
                "AND json_extract(state,'$.status') NOT IN ('accepted','blocked') "
                "ORDER BY COALESCE((SELECT max(e.at) FROM events e WHERE e.run=r.id AND e.kind='dispatched'),r.created),r.created,r.id"
            )
        )

    def last_transition(self) -> float | None:
        value = self.db.execute("SELECT max(at) FROM events").fetchone()[0]
        return None if value is None else float(value)

    def portfolio(self, identifier: str) -> str | None:
        row = self.db.execute(
            "SELECT document FROM portfolios WHERE id=?", (identifier,)
        ).fetchone()
        return None if row is None else str(row[0])

    def bind_portfolio(self, identifier: str, document: str) -> None:
        old = self.portfolio(identifier)
        if old is not None and old != document:
            raise Conflict("Approved portfolio revision is immutable")
        self.db.execute(
            "INSERT OR IGNORE INTO portfolios VALUES(?,?,?)",
            (identifier, digest(document), document),
        )

    def set_policy(self, workspace: str, mandatory: tuple[str, ...]) -> None:
        self.db.execute(
            "INSERT INTO project_policies VALUES(?,?) ON CONFLICT(workspace) DO UPDATE SET mandatory=excluded.mandatory",
            (workspace, canonical(sorted(set(mandatory)))),
        )
