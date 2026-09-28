"""SQLite state, journal and outbox share one atomic commit boundary."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import canonical, digest, run_json, run_load, workflow_json, workflow_load
from sdd_core.graph import validate
from sdd_core.models import Run, Transition, Workflow
from sdd_core.ports import Conflict as Conflict
from sdd_core.ports import UnitOfWork

from sdd_storage.unit import SQLiteUnit

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS definitions(digest TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL,
    workspace TEXT NOT NULL, context TEXT NOT NULL, claim TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dependencies(run TEXT NOT NULL REFERENCES runs(id),
    prerequisite TEXT NOT NULL REFERENCES runs(id), PRIMARY KEY(run,prerequisite));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
    version INTEGER NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY, run TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER, created REAL, packet TEXT,
    host_nonce TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS results(attempt TEXT PRIMARY KEY, digest TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, request TEXT NOT NULL, response TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_requests (attempt TEXT PRIMARY KEY REFERENCES effects(id),
    backend TEXT NOT NULL, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run,id);
CREATE INDEX IF NOT EXISTS effects_status ON effects(status);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS bindings(run TEXT NOT NULL, handler TEXT NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY(run,handler));
CREATE TABLE IF NOT EXISTS project_policies(workspace TEXT PRIMARY KEY, mandatory TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolios(id TEXT PRIMARY KEY, digest TEXT NOT NULL, document TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as probe:
                exists = probe.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                version = probe.execute("SELECT version FROM meta").fetchone() if exists else None
            if version and version[0] == 1:
                self.backup(self.path.with_name(self.path.name + f".pre-v2-{time.time_ns()}.bak"))
        with self.transaction() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            row = db.execute("SELECT version FROM meta").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES(1)")
            elif row[0] not in (1, 2):
                raise ValueError("Unsupported database version; restore a compatible backup")
            for statement in MIGRATION_2.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("UPDATE meta SET version=2")

    @contextmanager
    def unit(self) -> Iterator[UnitOfWork]:
        with self.transaction() as db:
            yield SQLiteUnit(self, db)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def publish(self, workflow: Workflow, mandatory: tuple[str, ...] = ()) -> str:
        validate(workflow, mandatory)
        document = workflow_json(workflow)
        identifier = digest(document)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO definitions VALUES(?,?)", (identifier, document))
        return identifier

    def workflow(self, identifier: str) -> Workflow:
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM definitions WHERE digest=?", (identifier,)
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return workflow_load(str(row[0]))

    def create(
        self,
        run: Run,
        workspace: str,
        context: str,
        claim: str,
        now: float,
        dependencies: tuple[str, ...] = (),
    ) -> Run:
        with self.transaction() as db:
            old = db.execute("SELECT * FROM runs WHERE id=?", (run.id,)).fetchone()
            if old:
                existing = run_load(str(old["state"]))
                deps = tuple(
                    str(r[0])
                    for r in db.execute(
                        "SELECT prerequisite FROM dependencies WHERE run=? ORDER BY prerequisite",
                        (run.id,),
                    )
                )
                if (
                    existing.workflow_digest,
                    old["workspace"],
                    old["context"],
                    old["claim"],
                    deps,
                ) != (
                    run.workflow_digest,
                    workspace,
                    context,
                    claim,
                    tuple(sorted(set(dependencies))),
                ):
                    raise Conflict("Run id reused with different input")
                return existing
            if (
                db.execute(
                    "SELECT 1 FROM definitions WHERE digest=?", (run.workflow_digest,)
                ).fetchone()
                is None
            ):
                raise KeyError(run.workflow_digest)
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (run.id, run_json(run), run.version, workspace, context, claim, now),
            )
            for prerequisite in sorted(set(dependencies)):
                if prerequisite == run.id:
                    raise ValueError("Self dependency")
                db.execute("INSERT INTO dependencies VALUES(?,?)", (run.id, prerequisite))
            db.execute(
                "INSERT INTO events(run,version,kind,at,detail,state) VALUES(?,?,?,?,?,?)",
                (run.id, 0, "created", now, "", run_json(run)),
            )
        return run

    @staticmethod
    def load(db: sqlite3.Connection, identifier: str) -> Run:
        row = db.execute("SELECT state FROM runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return run_load(str(row[0]))

    def get(self, identifier: str) -> Run:
        with self.transaction() as db:
            return self.load(db, identifier)

    @staticmethod
    def apply(db: sqlite3.Connection, before: Run, transition: Transition) -> Run:
        after = transition.state
        if after.id != before.id or after.version != before.version + 1:
            raise ValueError("Invalid transition version")
        cursor = db.execute(
            "UPDATE runs SET state=?,version=? WHERE id=? AND version=?",
            (run_json(after), after.version, after.id, before.version),
        )
        if cursor.rowcount != 1:
            raise Conflict("Stale state version")
        for event in transition.events:
            db.execute(
                "INSERT INTO events(run,version,kind,at,detail,state) VALUES(?,?,?,?,?,?)",
                (after.id, after.version, event.kind, event.at, event.detail, run_json(after)),
            )
        for effect in transition.effects:
            db.execute(
                "INSERT INTO effects(id,run,kind,payload,status) VALUES(?,?,?,?,?)",
                (effect.id, after.id, effect.kind, canonical(asdict(effect)), "pending"),
            )
        return after

    def history(self, identifier: str, after: int = 0, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid page size")
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,run,version,kind,at,detail FROM events WHERE run=? AND id>? ORDER BY id LIMIT ?",
                    (identifier, after, limit),
                )
            ]

    def backup(self, target: Path) -> None:
        if target.exists():
            raise FileExistsError(target)
        with (
            closing(sqlite3.connect(self.path)) as source,
            closing(sqlite3.connect(target)) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity failure")

    def replay(self, identifier: str) -> Run:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT version,state FROM events WHERE run=? ORDER BY id", (identifier,)
            ).fetchall()
            if not rows:
                raise KeyError(identifier)
            versions = [int(row[0]) for row in rows]
            if versions != list(range(len(versions))):
                raise ValueError("Journal gap")
            reconstructed = run_load(str(rows[-1][1]))
            if reconstructed != self.load(db, identifier):
                raise ValueError("Projection differs from journal")
            return reconstructed
