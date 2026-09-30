"""Isolated working copies ("lanes"): one git worktree per task, as in Orca or a
worktree-per-task factory.

A lane lives in the engine's work folder, outside the project. Every repository in
the run's scope is a git worktree on branch `ffai/<run>`, created from the
repository's current HEAD when the run first starts. Everything else in the
workspace is linked (a junction on Windows, a symlink elsewhere) so tools and
project checks see the same layout; linked folders are other checkouts and must not
be edited. The project itself gets the branch and, in its `.git`, the worktree's
registration: no file of its working tree is written.

Opening, linking dependencies and removing a lane live here; commit, rebase and integrate
are lane steps (`lane_actions`).
"""

import os
import shutil
import sys
from pathlib import Path

from sdd_runtime.files import atomic_write
from sdd_runtime.git import git, is_repository_top
from sdd_runtime.lane_model import (
    Attached,
    Lane,
    LaneRepo,
    lane_branch,
    load_lane,
)
from sdd_runtime.submodules import links_of

SKIP = {".git"}


def link_folder(target: Path, link: Path) -> None:
    if link.exists() or os.path.lexists(link):
        return
    if target.is_dir():
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))  # no privilege needed
        else:
            os.symlink(target, link, target_is_directory=True)
    else:
        shutil.copy2(target, link)  # root files are small; a copy cannot leak edits back


def _mirror(source: Path, lane: Path, scope: set[Path]) -> None:
    """Recreate `source` inside `lane`: scoped paths stay free for worktrees,
    folders that contain scoped paths are real, everything else is linked."""
    lane.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in SKIP:
            continue
        if entry in scope:
            continue
        if any(entry in path.parents for path in scope):
            _mirror(entry, lane / entry.name, scope)
        else:
            link_folder(entry, lane / entry.name)


def open_lane(
    workspace: Path, run_id: str, scope: tuple[Path, ...], root: Path, record: Path
) -> Lane:
    """Create (or finish creating) the lane at `root` and write its `record`; safe to
    repeat after a crash."""
    workspace = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    branch = lane_branch(run_id)
    scoped = {path.resolve() for path in scope} or {workspace}
    repos: list[LaneRepo] = []
    for path in sorted(scoped):
        relative = path.relative_to(workspace).as_posix() if path != workspace else "."
        target = root if path == workspace else root / relative
        if path.exists() and not is_repository_top(path):
            raise ValueError(
                f"Scope {relative} is not a git repository top; it cannot get a worktree"
            )
        if not path.exists():
            # The task creates this repository: it starts as a plain local repository.
            target.mkdir(parents=True, exist_ok=True)
            if not (target / ".git").exists():
                git(target, "init", "-q")
            repos.append(LaneRepo(relative, branch, "", "", True))
            continue
        base = git(path, "rev-parse", "HEAD").stdout.strip()
        origin = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        if not (target / ".git").exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            exists = (
                git(path, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
            )
            if exists:
                git(path, "worktree", "add", str(target), branch)
            else:
                git(path, "worktree", "add", "-b", branch, str(target), base)
        repos.append(LaneRepo(relative, branch, base, origin))
    if scoped != {workspace}:
        _mirror(workspace, root, scoped)
    lane = Lane(run_id, str(workspace), str(root), repos)
    atomic_write(record, lane.document())
    attach_links(lane)
    atomic_write(record, lane.document())
    return lane


def attach_links(lane: Lane) -> bool:
    """Check out every linked dependency where the lane's repositories link it, at its
    pinned commit, so the lane builds and runs as the workspace does. Idempotent; True
    when something was attached. A dependency that cannot be attached is skipped."""
    workspace, root = Path(lane.workspace), Path(lane.root)
    known = {item.path for item in lane.attached}
    before = len(lane.attached)
    for repo in lane.repos:
        if repo.fresh:
            continue
        for link in links_of(str(root), repo.path).links(str(root), repo.path):
            place = link.path if repo.path == "." else f"{repo.path}/{link.path}"
            target, source = root / place, workspace / link.dependency
            if place in known or not link.pinned or not is_repository_top(source):
                continue
            if target.is_dir() and any(target.iterdir()):
                continue  # already populated, as by an initialised submodule
            if target.is_dir():
                target.rmdir()
            added = git(
                source, "worktree", "add", "--detach", str(target), link.pinned, check=False
            )
            if added.returncode == 0:
                lane.attached.append(Attached(place, link.dependency))
                known.add(place)
    return len(lane.attached) != before


def remove_lane(lane: Lane, record: Path) -> None:
    """Delete worktrees, merged branches and links; never follow a link. The run's
    attempt files are not in the lane, so they stay."""
    if record.is_file():
        lane = load_lane(record.read_text(encoding="utf-8"))  # the lane's latest record
    workspace, root = Path(lane.workspace), Path(lane.root)
    # Attached dependencies first: they live inside the repositories' worktrees.
    for attached in sorted(lane.attached, key=lambda a: a.path.count("/"), reverse=True):
        source = workspace / attached.dependency
        git(source, "worktree", "remove", "--force", str(root / attached.path), check=False)
        git(source, "worktree", "prune", check=False)
    for repo in lane.repos:
        if repo.fresh:
            continue
        main, work = lane.main(repo), lane.work(repo)
        if work.exists() and not os.path.isjunction(work) and not work.is_symlink():
            git(main, "worktree", "remove", "--force", str(work), check=False)
        git(main, "worktree", "prune", check=False)
        git(main, "branch", "-d", repo.branch, check=False)  # only if merged
    _unlink_tree(root)
    record.unlink(missing_ok=True)


def _unlink_tree(path: Path) -> None:
    if not path.exists() and not os.path.lexists(path):
        return
    if os.path.isjunction(path) or path.is_symlink():
        os.unlink(path) if path.is_symlink() else os.rmdir(path)
        return
    if path.is_dir():
        for child in path.iterdir():
            _unlink_tree(child)
        path.rmdir()
    else:
        path.unlink()
