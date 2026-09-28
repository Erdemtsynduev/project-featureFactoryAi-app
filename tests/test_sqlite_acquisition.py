import sqlite3

import pytest
from sdd_storage.store import Store


def io_error(code=sqlite3.SQLITE_IOERR_SHMOPEN):
    error = sqlite3.OperationalError("injected disk I/O error")
    error.sqlite_errorcode = code
    error.sqlite_errorname = "SQLITE_IOERR_SHMOPEN"
    return error


@pytest.mark.parametrize(
    "statement", ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "BEGIN IMMEDIATE"]
)
def test_acquisition_reopens_connection_without_replaying_body(tmp_path, monkeypatch, statement):
    store = Store(tmp_path / "state.db")
    connect = sqlite3.connect
    opened, delays = [], []

    class Connection(sqlite3.Connection):
        closed = False

        def execute(self, sql, *args):
            if sql == statement and len(opened) <= 2:
                raise io_error()
            return super().execute(sql, *args)

        def close(self):
            self.closed = True
            return super().close()

    def flaky(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        opened.append(connection)
        return connection

    monkeypatch.setattr("sdd_storage.store.sqlite3.connect", flaky)
    monkeypatch.setattr("sdd_storage.store.time.sleep", delays.append)
    bodies = []
    with store.transaction(initialize=statement == "PRAGMA journal_mode=WAL") as db:
        bodies.append(1)
        db.execute("INSERT INTO commands VALUES('once','request','response')")
    assert bodies == [1] and len(opened) == 3 and all(db.closed for db in opened)
    assert delays == [0.05, 0.1]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


@pytest.mark.parametrize(
    "code,attempts",
    [(sqlite3.SQLITE_IOERR, 6), (sqlite3.SQLITE_FULL, 1), (sqlite3.SQLITE_CORRUPT, 1)],
)
def test_persistent_acquisition_failure_is_bounded(tmp_path, monkeypatch, code, attempts):
    store = Store(tmp_path / "state.db")
    calls, delays = [], []
    error = io_error(code)

    def broken(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr("sdd_storage.store.sqlite3.connect", broken)
    monkeypatch.setattr("sdd_storage.store.time.sleep", delays.append)
    with pytest.raises(sqlite3.OperationalError) as caught, store.transaction():
        pytest.fail("Transaction body must not run")
    assert caught.value is error and len(calls) == attempts
    assert len(delays) == attempts - 1


@pytest.mark.parametrize("stage", ["body", "commit"])
def test_io_error_after_begin_is_never_replayed(tmp_path, monkeypatch, stage):
    store = Store(tmp_path / "state.db")
    connect = sqlite3.connect
    bodies, delays = [], []
    error = io_error()

    class Connection(sqlite3.Connection):
        def commit(self):
            if stage == "commit":
                super().commit()  # An uncertain COMMIT can already be durable.
                raise error
            return super().commit()

    monkeypatch.setattr(
        "sdd_storage.store.sqlite3.connect", lambda *a, **kw: connect(*a, **kw, factory=Connection)
    )
    monkeypatch.setattr("sdd_storage.store.time.sleep", delays.append)
    with pytest.raises(sqlite3.OperationalError) as caught, store.transaction() as db:
        bodies.append(1)
        db.execute("INSERT INTO commands VALUES('once','request','response')")
        if stage == "body":
            raise error
    assert caught.value is error and bodies == [1] and delays == []
    with connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == int(stage == "commit")


def test_wal_is_persistent_and_not_reconfigured_per_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("sdd_storage.store.sqlite3.connect", traced)
    for _ in range(3):
        with store.transaction() as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert "PRAGMA journal_mode=WAL" not in statements
