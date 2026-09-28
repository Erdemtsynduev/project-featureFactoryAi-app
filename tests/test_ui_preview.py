import hashlib
import json
import sqlite3

import pytest
from sdd_ui.preview import snapshot
from sdd_ui.service import WorkspaceService


def test_portfolio_snapshot_is_read_only_and_never_becomes_runs(tmp_path):
    source = tmp_path / "legacy.db"
    portfolio = {
        "revision": 42,
        "sources": {
            "104:A": {"plan": "104", "requirement": "Проверить подъём", "path": "104.md"},
            "105:B": {"plan": "105", "requirement": "Готовая работа", "mark": "x"},
        },
        "units": {
            "104:A.1": {
                "status": "blocked",
                "problem": "Нужен ответ",
                "contract": {
                    "source_id": "104:A",
                    "acceptance": "All checks pass",
                    "depends_on": [],
                },
            }
        },
    }
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE portfolio(id INTEGER PRIMARY KEY, data TEXT)")
        db.execute("INSERT INTO portfolio VALUES(1,?)", (json.dumps(portfolio),))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = snapshot(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["revision"] == 42
    assert len(result["items"]) == 2
    assert result["items"][0]["title"] == "Проверить подъём"
    assert result["items"][1]["status"] == "accepted"
    (tmp_path / "ui.preview.json").write_text(json.dumps(result), encoding="utf-8")
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        assert service.state()["preview"] == result
        assert service.state()["runs"] == []
        assert not service.settings["running"]
        assert not service.coordinator.live
    finally:
        service.coordinator.close()


def test_missing_source_is_not_created(tmp_path):
    source = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        snapshot(source)
    assert not source.exists()
