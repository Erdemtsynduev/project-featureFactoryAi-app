"""Read-only legacy portfolio snapshots. Never create engine runs from imported data."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sdd_core.codec import object_json, sequence, text
from sdd_core.models import Json
from sdd_runtime.files import atomic_write


def snapshot(source: Path) -> dict[str, Json]:
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT data FROM portfolio WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Portfolio has no state")
    data = object_json(str(row[0]))
    sources = data.get("sources", {})
    units = data.get("units", {})
    if not isinstance(sources, dict) or not isinstance(units, dict):
        raise ValueError("Invalid portfolio")
    items: list[Json] = []
    covered: set[str] = set()
    for identifier, unit in units.items():
        if not isinstance(unit, dict) or not isinstance(unit.get("contract"), dict):
            raise ValueError("Invalid unit contract")
        contract = unit["contract"]
        assert isinstance(contract, dict)
        source_id = str(contract.get("source_id", identifier))
        covered.add(source_id)
        origin = sources.get(source_id, {})
        origin = origin if isinstance(origin, dict) else {}
        status = str(unit.get("status", "ready"))
        items.append(
            {
                "id": identifier,
                "plan": str(origin.get("plan", identifier.split(":")[0])),
                "title": str(origin.get("requirement", contract.get("acceptance", identifier))),
                "context": str(contract.get("acceptance", "")),
                "status": status,
                "reason": str(unit.get("problem", "")),
                "dependencies": contract.get("depends_on", []),
                "path": str(origin.get("path", "")),
                "kind": "ticket",
            }
        )
    for identifier, origin in sources.items():
        if identifier in covered or not isinstance(origin, dict):
            continue
        items.append(
            {
                "id": identifier,
                "plan": str(origin.get("plan", "")),
                "title": str(origin.get("requirement", identifier)),
                "context": "\n".join(str(v) for v in sequence(origin.get("detail", []))),
                "status": "accepted" if origin.get("mark") == "x" else "ready",
                "reason": "",
                "dependencies": [],
                "path": str(origin.get("path", "")),
                "kind": "requirement",
            }
        )
    return {
        "source": str(source.resolve()),
        "captured_at": datetime.now(UTC).isoformat(),
        "revision": data.get("revision"),
        "items": items,
    }


def load_preview(path: Path) -> dict[str, Json]:
    if not path.exists():
        return {"items": []}
    doc = object_json(path.read_text(encoding="utf-8"))
    for item in sequence(doc.get("items", [])):
        if not isinstance(item, dict):
            raise ValueError("Invalid preview card")
        text(item.get("id"), "id")
        text(item.get("title"), "title")
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    destination = args.database.resolve().with_suffix(".preview.json")
    if destination.exists():
        raise ValueError("Snapshot already exists; choose a new preview database")
    result = snapshot(args.source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(destination, json.dumps(result, ensure_ascii=False))
    print(f"Snapshot saved: {destination}; {len(sequence(result['items']))} cards; no runs created")


if __name__ == "__main__":
    main()
