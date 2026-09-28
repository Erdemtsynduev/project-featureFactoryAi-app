"""Archive terminal diagnostics only; never remove acceptance evidence or active work."""

import gzip
import time
from pathlib import Path

from sdd_core.codec import result_load
from sdd_core.ports import StateStore


def archive_diagnostics(store: StateStore, older_than_days: int = 30) -> tuple[str, ...]:
    if older_than_days < 1:
        raise ValueError("Retention must be positive")
    cutoff = time.time() - older_than_days * 86400
    archived: list[str] = []
    with store.unit() as db:
        rows = db.effects(("done",))
    for row in rows:
        root = Path(row.workspace)
        folder = root / ".sdd-engine" / row.run_id / row.id
        result = result_load(row.receipt or "{}")
        protected = {(root / artifact.path).resolve() for artifact in result.artifacts}
        for path in folder.glob("*.log"):
            if path.resolve() in protected or path.stat().st_mtime >= cutoff:
                continue
            target = path.with_suffix(path.suffix + ".gz")
            if target.exists():
                continue
            data = path.read_bytes()
            target.write_bytes(gzip.compress(data, mtime=0))
            if gzip.decompress(target.read_bytes()) != data:
                raise RuntimeError("Archive verification failed")
            path.unlink()
            archived.append(str(target))
    return tuple(archived)
