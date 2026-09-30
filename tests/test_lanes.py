"""Worktree lanes: isolation, fast-forward merge, rebase, conflicts and cleanup."""

import os
import subprocess
from pathlib import Path

import pytest
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.lanes import (
    LANE_FILE,
    LaneJob,
    integrate,
    load_lane,
    open_lane,
    rebase,
    remove_lane,
)
from sdd_storage.store import Store


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(path: Path, content: str = "start\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "T")
    (path / "file.txt").write_text(content, encoding="utf-8")
    (path / "NAME").write_text(path.name, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "meta"
    root.mkdir()
    (root / "AGENTS.md").write_text("rules", encoding="utf-8")
    repository(root / "libraries" / "alpha")
    repository(root / "libraries" / "beta")
    repository(root / "tools")
    return root


def commit(repo: Path, content: str, message: str = "work") -> str:
    (repo / "file.txt").write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_lane_isolates_scoped_repos_and_links_the_rest(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t1", (alpha,))
    root = Path(lane.root)
    work = root / "libraries" / "alpha"
    assert git(work, "branch", "--show-current") == "ffai/t1"
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "rules"
    for linked in (root / "tools", root / "libraries" / "beta"):
        assert os.path.isjunction(linked) or linked.is_symlink()
        assert (linked / "NAME").exists()
    (work / "file.txt").write_text("lane only\n", encoding="utf-8")
    assert (alpha / "file.txt").read_text(encoding="utf-8") == "start\n"
    assert load_lane((root / LANE_FILE).read_text(encoding="utf-8")) == lane
    assert open_lane(workspace, "t1", (alpha,)) == lane  # idempotent after a crash
    assert git(work, "status", "--porcelain") == "M file.txt"  # bookkeeping excluded


def test_integrate_fast_forwards_or_asks_for_rebase(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t2", (alpha,))
    work = Path(lane.root) / "libraries" / "alpha"
    (work / "file.txt").write_text("uncommitted\n", encoding="utf-8")
    assert integrate(lane)[0] == "behind"
    assert rebase(lane, LaneJob("t2"))[0] == "done"
    outcome, reason = integrate(lane)
    assert outcome == "merged" and "libraries/alpha" in reason
    assert git(alpha, "rev-parse", "HEAD") == git(work, "rev-parse", "HEAD")
    assert (alpha / "file.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_moved_base_is_rebased_then_merged(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t3", (alpha,))
    work = Path(lane.root) / "libraries" / "alpha"
    (work / "other.txt").write_text("lane feature", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "feature")
    base = commit(alpha, "start\nmain moved\n")
    assert integrate(lane)[0] == "behind"
    outcome, reason = rebase(lane, LaneJob("t3"))
    assert outcome == "done" and base[:10] in reason
    assert integrate(lane)[0] == "merged"
    assert (alpha / "other.txt").exists() and "main moved" in (alpha / "file.txt").read_text()


def test_conflict_stops_until_resolved(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t4", (alpha,))
    work = Path(lane.root) / "libraries" / "alpha"
    commit(work, "lane version\n")
    commit(alpha, "main version\n")
    outcome, reason = rebase(lane, LaneJob("t4"))
    assert outcome == "conflict" and "file.txt" in reason
    assert rebase(lane, LaneJob("t4"))[0] == "conflict"  # still unresolved
    (work / "file.txt").write_text("main version\nlane version\n", encoding="utf-8")
    git(work, "add", "file.txt")
    assert rebase(lane, LaneJob("t4"))[0] == "done"
    assert integrate(lane)[0] == "merged"
    assert (alpha / "file.txt").read_text() == "main version\nlane version\n"


def test_main_copy_on_another_branch_blocks_merge(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t5", (alpha,))
    git(alpha, "switch", "-q", "-c", "elsewhere")
    assert integrate(lane)[0] == "blocked"


def test_new_repository_moves_into_the_workspace(workspace):
    planned = workspace / "libraries" / "gamma"
    lane = open_lane(workspace, "t6", (planned,))
    work = Path(lane.root) / "libraries" / "gamma"
    assert lane.repos[0].fresh and (work / ".git").exists() and not planned.exists()
    repository(work)
    assert integrate(lane)[0] == "merged"
    assert (planned / "NAME").read_text() == "gamma"


def test_cleanup_removes_lane_but_never_linked_checkouts(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = open_lane(workspace, "t7", (alpha,))
    root = Path(lane.root)
    work = root / "libraries" / "alpha"
    commit(work, "done\n")
    assert integrate(lane)[0] == "merged"
    (root / ".sdd-engine" / "t7").mkdir(parents=True)
    (root / ".sdd-engine" / "t7" / "receipt.json").write_text("{}", encoding="utf-8")
    remove_lane(lane)
    assert not root.exists()
    assert (workspace / "libraries" / "beta" / "NAME").exists()
    assert (workspace / "tools" / "NAME").exists()
    assert (workspace / ".sdd-engine" / "t7" / "receipt.json").exists()
    assert "ffai/t7" not in git(alpha, "branch")
    assert git(alpha, "worktree", "list").count("\n") == 0


def test_coordinator_moves_isolated_runs_into_parallel_lanes(tmp_path, workspace):
    store = Store(tmp_path / "state.db")
    engine = Engine(store)
    flow = Workflow(
        "isolated",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "integrate"),), mutates=True),
            Step("integrate", "operation", "lane-integrate", transitions=(("merged", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    for run_id in ("a", "b"):
        run = engine.create(run_id, definition, workspace, "", None, 0, scope=("libraries/alpha",))
        engine.command(run_id, "resume", run_id + "-r", run.version, 1)
    coordinator = Coordinator(engine, Registry())
    try:
        coordinator.lanes.open("a", 2)
        coordinator.lanes.open("b", 2)
        coordinator.lanes.open("a", 3)  # already open: no change
        with store.unit() as db:
            locations = {run_id: db.location(run_id) for run_id in ("a", "b")}
        assert locations["a"][0].endswith(os.path.join(".sdd-lanes", "a"))
        assert not engine.workspace.overlaps(locations["a"][1], locations["b"][1])
        run = engine.store.get("a")
        assert run.revision == engine.observe("a") and run.version == 2
        assert [e["kind"] for e in store.history("a")][-1] == "lane_opened"
    finally:
        coordinator.close()
