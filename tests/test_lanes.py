"""Worktree lanes: isolation, fast-forward merge, rebase, conflicts and cleanup."""

import os
import subprocess
from pathlib import Path

import pytest
from sdd_core.models import Step, Workflow
from sdd_core.sdk import Registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.lane_actions import integrate, rebase
from sdd_runtime.lane_model import Lane, LaneJob, lane_file, load_lane
from sdd_runtime.lanes import open_lane, remove_lane
from sdd_storage.store import Store


def record_of(workspace: Path, run: str) -> Path:
    """Where the engine keeps the lane's record: in its own folder, beside the workspace."""
    return lane_file(workspace.parent / "engine.work" / "engine" / run)


def opened(workspace: Path, run: str, scope: tuple[Path, ...]) -> Lane:
    """A lane where the engine opens them: outside the workspace."""
    root = workspace.parent / "engine.work" / "lanes" / run
    return open_lane(workspace, run, scope, root, record_of(workspace, run))


def removed(lane: Lane) -> None:
    remove_lane(lane, record_of(Path(lane.workspace), lane.run))


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
    lane = opened(workspace, "t1", (alpha,))
    root = Path(lane.root)
    work = root / "libraries" / "alpha"
    assert git(work, "branch", "--show-current") == "ffai/t1"
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "rules"
    for linked in (root / "tools", root / "libraries" / "beta"):
        assert os.path.isjunction(linked) or linked.is_symlink()
        assert (linked / "NAME").exists()
    (work / "file.txt").write_text("lane only\n", encoding="utf-8")
    assert (alpha / "file.txt").read_text(encoding="utf-8") == "start\n"
    assert load_lane(record_of(workspace, "t1").read_text(encoding="utf-8")) == lane
    assert opened(workspace, "t1", (alpha,)) == lane  # idempotent after a crash
    assert git(work, "status", "--porcelain") == "M file.txt"  # no engine file in the copy
    # The project's working tree is untouched: no folder, no ignore rule, only the branch.
    assert not root.is_relative_to(workspace)
    assert sorted(p.name for p in workspace.iterdir()) == ["AGENTS.md", "libraries", "tools"]
    assert git(alpha, "status", "--porcelain") == ""
    assert "sdd" not in (alpha / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_integrate_fast_forwards_or_asks_for_rebase(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = opened(workspace, "t2", (alpha,))
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
    lane = opened(workspace, "t3", (alpha,))
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
    lane = opened(workspace, "t4", (alpha,))
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
    lane = opened(workspace, "t5", (alpha,))
    git(alpha, "switch", "-q", "-c", "elsewhere")
    assert integrate(lane)[0] == "blocked"


def test_new_repository_moves_into_the_workspace(workspace):
    planned = workspace / "libraries" / "gamma"
    lane = opened(workspace, "t6", (planned,))
    work = Path(lane.root) / "libraries" / "gamma"
    assert lane.repos[0].fresh and (work / ".git").exists() and not planned.exists()
    repository(work)
    assert integrate(lane)[0] == "merged"
    assert (planned / "NAME").read_text() == "gamma"


def test_cleanup_removes_lane_but_never_linked_checkouts(workspace):
    alpha = workspace / "libraries" / "alpha"
    lane = opened(workspace, "t7", (alpha,))
    root = Path(lane.root)
    work = root / "libraries" / "alpha"
    commit(work, "done\n")
    assert integrate(lane)[0] == "merged"
    receipt = record_of(workspace, "t7").parent / "attempt" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}", encoding="utf-8")
    removed(lane)
    assert not root.exists() and not record_of(workspace, "t7").exists()
    assert (workspace / "libraries" / "beta" / "NAME").exists()
    assert (workspace / "tools" / "NAME").exists()
    assert receipt.exists(), "the run's attempt files stay"
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
        assert Path(locations["a"][0]) == tmp_path / "state.work" / "lanes" / "a"
        assert lane_file(tmp_path / "state.work" / "engine" / "a").is_file()
        assert not engine.workspace.overlaps(locations["a"][1], locations["b"][1])
        run = engine.store.get("a")
        assert run.revision == engine.observe("a") and run.version == 2
        assert [e["kind"] for e in store.history("a")][-1] == "lane_opened"
    finally:
        coordinator.close()


def test_a_lane_document_round_trips_and_an_unversioned_one_is_version_one(workspace):
    lane = opened(workspace, "t8", (workspace / "libraries" / "alpha",))
    assert load_lane(lane.document()) == lane
    old = load_lane('{"run":"x","workspace":"w","root":"r","repos":[]}')
    assert old.version == 1 and old.attached == [] and old.status == "active"


def test_a_ticket_is_delivered_without_writing_engine_files_into_the_project(tmp_path):
    """End to end through real processes: work in a lane, commit, merge, clean up. The
    project gets the commit and nothing else."""
    import sys
    import time

    from sdd_core.codec import canonical
    from sdd_runtime.composition import registry

    project = tmp_path / "project"
    app = repository(project / "app")
    (project / "NOTES.md").write_text("notes", encoding="utf-8")
    exclude_before = (app / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    engine = Engine(Store(tmp_path / "ui.db"))
    write = canonical(
        {"argv": [sys.executable, "-c", "open('feature.txt', 'w').write('done')"], "cwd": "app"}
    )
    flow = Workflow(
        "ticket",
        "work",
        (
            Step(
                "work",
                "operation",
                "command",
                transitions=(("done", "commit"),),
                mutates=True,
                config=write,
            ),
            Step("commit", "operation", "lane-commit", transitions=(("done", "integrate"),)),
            Step("integrate", "operation", "lane-integrate", transitions=(("merged", "finish"),)),
            Step("finish", "finish"),
        ),
    )
    run = engine.create(
        "t1",
        engine.store.publish(flow),
        project,
        "Ticket T1: Add the feature",
        None,
        0,
        (),
        ("app",),
    )
    engine.command("t1", "resume", "go", run.version, 1)
    coordinator = Coordinator(engine, registry(None))
    try:
        deadline = time.monotonic() + 60
        while engine.store.get("t1").status != "accepted" and time.monotonic() < deadline:
            coordinator.tick()
            time.sleep(0.1)
        state = engine.store.get("t1")
        assert state.status == "accepted", state.reason
        coordinator.tick()  # acceptance removes the lane
    finally:
        coordinator.close()

    # The project: the merged commit, a clean tree, and not one file or rule of the engine.
    assert (app / "feature.txt").read_text(encoding="utf-8") == "done"
    assert git(app, "log", "-1", "--format=%s") == "feat(app): Add the feature"
    assert git(app, "status", "--porcelain") == ""
    assert sorted(p.name for p in project.iterdir()) == ["NOTES.md", "app"]
    assert (app / ".git" / "info" / "exclude").read_text(encoding="utf-8") == exclude_before
    assert git(app, "worktree", "list").count("\n") == 0 and "ffai/t1" not in git(app, "branch")
    # The engine's own folder, beside its database: the attempts stay, the lane is gone.
    work = tmp_path / "ui.work"
    attempts = [p for p in (work / "engine" / "t1").iterdir() if p.is_dir()]
    assert len(attempts) == 3 and all((p / "receipt.json").is_file() for p in attempts)
    assert not (work / "lanes" / "t1").exists()


def test_the_work_folder_can_be_moved_to_a_short_path(tmp_path, monkeypatch):
    from sdd_runtime.composition import WORK_VARIABLE, local_engine

    beside = local_engine(tmp_path / "ui.db")
    assert Path(beside.workspace.folder("t1", "a")) == tmp_path / "ui.work" / "engine" / "t1" / "a"
    assert Path(beside.workspace.lane("t1")) == tmp_path / "ui.work" / "lanes" / "t1"
    monkeypatch.setenv(WORK_VARIABLE, str(tmp_path / "w"))
    moved = local_engine(tmp_path / "other.db")
    assert Path(moved.workspace.lane("t1")) == tmp_path / "w" / "lanes" / "t1"
