"""One-time rewrites of stored data, applied by the store when it opens an older
database (it backs the database up first). Each rewrite is keyed by the schema
version that introduced it, so a database takes each once, in order."""

import sqlite3
from collections.abc import Callable

from sdd_core import machine
from sdd_core.codec import canonical, decode, object_json
from sdd_core.models import Cause, Run

# Reason texts of rules, as runs recorded them before `cause` was stored.
_CAUSES: dict[str, Cause] = {
    machine.CALL_LIMIT: "call_limit",
    machine.PLANNING_LIMIT: "planning_limit",
    machine.TOKEN_LIMIT: "token_limit",
    machine.VISIT_LIMIT: "visit_limit",
    machine.QUEUE_LIMIT: "queue_limit",
    machine.WAIT_RETRY_LIMIT: "wait_limit",
    machine.WORKSPACE_CHANGED: "workspace_changed",
}


def cause_of(run: Run) -> Cause:
    """The cause of a run stored before causes existed, read from its reason text."""
    if run.reason == machine.STOP_REQUESTED:
        return "stop"
    if run.status != "blocked":
        return ""
    if run.reason in _CAUSES:
        return _CAUSES[run.reason]
    if run.reason.startswith(machine.UNCERTAIN):
        return "uncertain"
    if run.reason.startswith(machine.UNSATISFIED):
        return "acceptance"
    return "blocked"


def explicit_causes(db: sqlite3.Connection) -> None:
    """Version 6: every stored run names its cause; none is inferred on read."""
    for identifier, state in db.execute("SELECT id,state FROM runs").fetchall():
        stored = object_json(str(state))
        if "cause" in stored:
            continue
        stored["cause"] = cause_of(decode(Run, stored))
        db.execute("UPDATE runs SET state=? WHERE id=?", (canonical(stored), identifier))


def feature_kinds(db: sqlite3.Connection) -> None:
    """Version 6: a feature is stored as kind `feature`; it was once `requirement`."""
    db.execute(
        "UPDATE ui_tasks SET document=json_set(document,'$.kind','feature') "
        "WHERE json_extract(document,'$.kind')='requirement'"
    )


def version_6(db: sqlite3.Connection) -> None:
    explicit_causes(db)
    feature_kinds(db)


def sources_instead_of_plans(db: sqlite3.Connection) -> None:
    """Version 7: work remembers the source it came from, not a plan.

    `plan` leaves every task record, whose `source` becomes the plan's file when it
    names none. A closed feature covers no rows any more: they were planned again.
    The plan summaries go; nothing follows a plan file after it is imported.
    """
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='ui_plans'").fetchone()
    paths: dict[tuple[str, str], str] = {}
    for project, identifier, document in (
        db.execute("SELECT project,id,document FROM ui_plans").fetchall() if exists else ()
    ):
        summary = object_json(str(document))
        paths[str(project), str(identifier)] = str(summary.get("path") or summary.get("url") or "")
    for identifier, document in db.execute("SELECT id,document FROM ui_tasks").fetchall():
        record = object_json(str(document))
        before = canonical(record)
        plan = str(record.pop("plan", ""))
        if plan and not record.get("source"):
            record["source"] = paths.get((str(record.get("project", "")), plan)) or "plan:" + plan
        if record.get("closed") is True:
            record.pop("rows", None)
        if canonical(record) != before:
            db.execute("UPDATE ui_tasks SET document=? WHERE id=?", (canonical(record), identifier))
    db.execute("DROP TABLE IF EXISTS ui_plans")


DATA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    6: version_6,
    7: sources_instead_of_plans,
}
