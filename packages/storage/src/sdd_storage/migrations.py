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


DATA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {6: version_6}
