"""SQLite implementation of the SQL-free application transaction port."""

import json
import sqlite3
from typing import TYPE_CHECKING

from sdd_core.codec import result_load, sequence, text
from sdd_core.models import Result, Run, Transition

from sdd_storage.runtime_records import SQLiteRuntimeRecords

if TYPE_CHECKING:
    from sdd_storage.store import Store


class SQLiteUnit(SQLiteRuntimeRecords):
    def set_context(self, identifier: str, context: str) -> None:
        if (
            self.db.execute("UPDATE runs SET context=? WHERE id=?", (context, identifier)).rowcount
            != 1
        ):
            raise KeyError(identifier)

    def __init__(self, store: "Store", db: sqlite3.Connection) -> None:
        super().__init__(db)
        self.store = store

    def run(self, identifier: str) -> Run:
        return self.store.load(self.db, identifier)

    def apply(self, before: Run, transition: Transition) -> Run:
        return self.store.apply(self.db, before, transition)

    def command(self, identifier: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT request,response FROM commands WHERE id=?", (identifier,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def save_command(self, identifier: str, request: str, response: str) -> None:
        self.db.execute("INSERT INTO commands VALUES(?,?,?)", (identifier, request, response))

    def location(self, identifier: str) -> tuple[str, str]:
        row = self.db.execute(
            "SELECT workspace,claim FROM runs WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return str(row[0]), str(row[1])

    def locations(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1])) for row in self.db.execute("SELECT id,workspace FROM runs")
        )

    def dependency_edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in self.db.execute("SELECT run,prerequisite FROM dependencies ORDER BY run")
        )

    def relocate(self, identifier: str, workspace: str, claim: str) -> None:
        if (
            self.db.execute(
                "UPDATE runs SET workspace=?, claim=? WHERE id=?", (workspace, claim, identifier)
            ).rowcount
            != 1
        ):
            raise KeyError(identifier)

    def policy(self, workspace: str) -> tuple[str, ...]:
        row = self.db.execute(
            "SELECT mandatory FROM project_policies WHERE workspace=?", (workspace,)
        ).fetchone()
        return () if row is None else tuple(text(x, "gate") for x in sequence(json.loads(row[0])))

    def dependencies(self, identifier: str) -> tuple[Run, ...]:
        return tuple(
            self.run(str(row[0]))
            for row in self.db.execute(
                "SELECT prerequisite FROM dependencies WHERE run=?", (identifier,)
            )
        )

    def active_claims(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in self.db.execute(
                "SELECT e.kind,r.claim FROM effects e JOIN runs r ON r.id=e.run "
                "WHERE e.status IN ('pending','running','uncertain') AND e.kind NOT IN ('human','condition')"
            )
        )

    def unfinished_claims(self, identifier: str) -> tuple[tuple[Run, str], ...]:
        rows = self.db.execute(
            "SELECT id,claim FROM runs WHERE id<>? AND json_extract(state,'$.generation')>0 "
            "AND json_extract(state,'$.status')<>'accepted'",
            (identifier,),
        ).fetchall()
        return tuple((self.run(str(row[0])), str(row[1])) for row in rows)

    def results(self, identifier: str) -> tuple[Result, ...]:
        return tuple(
            result_load(str(row[0]))
            for row in self.db.execute(
                "SELECT r.document FROM results r JOIN effects e ON e.id=r.attempt WHERE e.run=?",
                (identifier,),
            )
        )

    def receipt(self, attempt: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT r.digest,e.run FROM results r JOIN effects e ON e.id=r.attempt WHERE r.attempt=?",
            (attempt,),
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def save_result(self, attempt: str, fingerprint: str, document: str) -> None:
        self.db.execute("INSERT INTO results VALUES(?,?,?)", (attempt, fingerprint, document))
        self.db.execute(
            "UPDATE effects SET status='done',receipt=? WHERE id=?", (document, attempt)
        )

    def effect_status(self, attempt: str, status: str) -> None:
        self.db.execute("UPDATE effects SET status=? WHERE id=?", (status, attempt))
