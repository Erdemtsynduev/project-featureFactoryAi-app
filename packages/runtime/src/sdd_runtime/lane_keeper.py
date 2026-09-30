"""Lifecycle of a run's isolated worktrees: intent first, then the repository change."""

from pathlib import Path

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.lane_model import LANE_VERSION, Lane, load_lane
from sdd_runtime.lanes import attach_links, open_lane, remove_lane


class LaneKeeper:
    def __init__(self, engine: ApplicationEngine) -> None:
        self.engine = engine
        # Runs whose lane is settled in this process; the database stays authoritative.
        self.cleaned: set[str] = set()

    def open(self, run_id: str, now: float) -> None:
        """Give the run its isolated worktrees before its first attempt (idempotent)."""
        with self.engine.store.unit() as db:
            document = db.lane(run_id)
            workspace, claim = db.location(run_id)
        known = load_lane(document) if document else None
        if known and known.status == "active" and Path(workspace) == Path(known.root):
            if known.version < LANE_VERSION:
                # A lane opened by an earlier release gains its dependencies once.
                attach_links(known)
                known.version = LANE_VERSION
                with self.engine.store.unit() as db:
                    db.save_lane(run_id, known.document())
            return
        main = Path(known.workspace) if known else Path(workspace)
        paths = self.engine.workspace.paths(claim)
        scope = () if paths == (str(main),) else tuple(Path(p) for p in paths)
        if known is None:
            # Record the intent before touching any repository.
            with self.engine.store.unit() as db:
                db.save_lane(run_id, Lane(run_id, str(main), "", status="opening").document())
        lane = open_lane(main, run_id, scope)
        lane_claim = self.engine.workspace.claim(
            lane.root, tuple(repo.path for repo in lane.repos if repo.path != ".")
        )
        with self.engine.store.unit() as db:
            db.save_lane(run_id, lane.document())
        self.engine.relocate(run_id, lane.root, lane_claim, now)

    def close(self, run_id: str) -> None:
        """After acceptance: worktrees, merged branches and links go; evidence stays."""
        with self.engine.store.unit() as db:
            document = db.lane(run_id)
        if document is None:
            self.cleaned.add(run_id)
            return
        lane = load_lane(document)
        if lane.status not in ("active", "removing") or not lane.root:
            self.cleaned.add(run_id)
            return
        # Intent first: a crash mid-removal is retried from "removing" on the next pass.
        lane.status = "removing"
        with self.engine.store.unit() as db:
            db.save_lane(run_id, lane.document())
        try:
            remove_lane(lane)
        except (OSError, RuntimeError):
            return  # retried on the next pass; a locked file must not stop the queue
        lane.status = "removed"
        with self.engine.store.unit() as db:
            db.save_lane(run_id, lane.document())
        self.cleaned.add(run_id)
