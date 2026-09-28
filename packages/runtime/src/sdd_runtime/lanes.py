"""Isolated working copies ("lanes"): one git worktree per task, as in Orca or a
worktree-per-task factory.

A lane lives at `<workspace>/.sdd-lanes/<run>/`. Every repository in the run's
scope is a git worktree on branch `ffai/<run>`, created from the repository's
current HEAD when the run first starts. Everything else in the workspace is
linked (a junction on Windows, a symlink elsewhere) so tools and project checks
see the same layout; linked folders are other checkouts and must not be edited.

Merging back is a workflow step, never implicit:
* `integrate` (read-only for the lane) fast-forwards each main checkout to the
  lane branch, or reports `behind` when the lane has uncommitted work or the
  base branch moved;
* `rebase` (mutating) commits leftover work and rebases onto the moved base,
  continuing an interrupted rebase, and reports `conflict` with the files when
  git stops. An agent (autonomous mode) or a human then resolves it in the lane.
After acceptance the coordinator removes worktrees, branches and links; evidence
is moved back into the workspace so history stays inspectable.
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sdd_core.codec import canonical, object_json, sequence, text
from sdd_core.models import Artifact, Result
from sdd_core.sdk import Launch, Manifest, Packet

from sdd_runtime.files import ENGINE_DIRECTORY, atomic_write, evidence
from sdd_runtime.platform import NO_WINDOW

LANES = ".sdd-lanes"
LANE_FILE = ".sdd-lane.json"
RESULT_FILE = "lane-result.json"
SKIP = {".git", LANES, ENGINE_DIRECTORY}
# Commits made on behalf of the engine need an identity even on fresh machines.
IDENTITY = ("-c", "user.name=Feature Factory", "-c", "user.email=feature-factory@localhost")


@dataclass
class LaneRepo:
    path: str  # relative to the workspace
    branch: str
    base: str  # commit the lane started from ("" for a repository the task creates)
    origin: str  # branch checked out in the main copy at creation ("" if detached)
    fresh: bool = False


@dataclass
class Lane:
    run: str
    workspace: str
    root: str
    repos: list[LaneRepo] = field(default_factory=list)
    status: str = "active"  # opening | active | removing | removed
    version: int = 1

    def document(self) -> str:
        return canonical(asdict(self))


def load_lane(document: str) -> Lane:
    raw = object_json(document)
    repos = [
        LaneRepo(
            text(item["path"], "path"),
            text(item["branch"], "branch"),
            text(item["base"], "base"),
            text(item["origin"], "origin"),
            item.get("fresh") is True,
        )
        for item in (dict(x) for x in sequence(raw.get("repos", [])))  # type: ignore[arg-type]
    ]
    return Lane(
        text(raw["run"], "run"),
        text(raw["workspace"], "workspace"),
        text(raw["root"], "root"),
        repos,
        text(raw.get("status", "active"), "status"),
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        creationflags=NO_WINDOW,
        env={**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        raise RuntimeError(
            f"git {' '.join(args[:2])}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def is_repository_top(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    top = git(path, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    return bool(top) and Path(top).resolve() == path.resolve()


def _link(target: Path, link: Path) -> None:
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
            _link(entry, lane / entry.name)


def open_lane(workspace: Path, run_id: str, scope: tuple[Path, ...]) -> Lane:
    """Create (or finish creating) the lane; safe to repeat after a crash."""
    workspace = workspace.resolve()
    root = workspace / LANES / run_id
    branch = "ffai/" + run_id
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
    atomic_write(root / LANE_FILE, lane.document())
    for repo in repos:
        _exclude(
            root if repo.path == "." else root / repo.path, (LANE_FILE, ENGINE_DIRECTORY + "/")
        )
    if is_repository_top(workspace):
        _exclude(workspace, (LANES + "/",))
    return lane


def _exclude(repo: Path, patterns: tuple[str, ...]) -> None:
    """Add patterns to the repository's local exclude file, never to .gitignore.

    Worktrees share `info/exclude` with their main repository, which is fine:
    the patterns only name engine bookkeeping.
    """
    admin = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    exclude = admin / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    additions = [p for p in patterns if p not in current.split()]
    if additions:
        exclude.write_text(current.rstrip() + "\n" + "\n".join(additions) + "\n", encoding="utf-8")


def _dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip())


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _rebasing(repo: Path) -> bool:
    admin = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    return (admin / "rebase-merge").exists() or (admin / "rebase-apply").exists()


def integrate(lane: Lane) -> tuple[str, str]:
    """Fast-forward main checkouts; `behind` when a rebase or commit must come first."""
    workspace, root = Path(lane.workspace), Path(lane.root)
    plans = []
    for repo in lane.repos:
        work = root if repo.path == "." else root / repo.path
        main = workspace if repo.path == "." else workspace / repo.path
        if _dirty(work):
            return "behind", f"{repo.path}: uncommitted work in the lane"
        if repo.fresh:
            if main.exists():
                return "blocked", f"{repo.path} appeared in the workspace meanwhile; merge by hand"
            plans.append((repo, work, main, ""))
            continue
        current = git(
            main, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        ).stdout.strip()
        if repo.origin and current != repo.origin:
            return (
                "blocked",
                f"{repo.path}: main copy is on {current or 'a detached HEAD'}, expected {repo.origin}",
            )
        head = git(main, "rev-parse", "HEAD").stdout.strip()
        tip = git(work, "rev-parse", "HEAD").stdout.strip()
        if not _ancestor(work, head, tip):
            return "behind", f"{repo.path}: base moved to {head[:10]}"
        plans.append((repo, work, main, tip))
    merged = []
    for repo, work, main, tip in plans:
        if repo.fresh:
            shutil.move(str(work), str(main))
            _link(main, work)
            merged.append(repo.path + " (new repository)")
            continue
        result = git(main, "merge", "--ff-only", "--no-edit", repo.branch, check=False)
        if result.returncode:
            return (
                "blocked",
                f"{repo.path}: main copy refused the merge: {result.stderr.strip()[:400]}",
            )
        merged.append(f"{repo.path} → {tip[:10]}")
    return "merged", "Merged: " + ", ".join(merged)


def rebase(lane: Lane, message: str) -> tuple[str, str]:
    """Commit leftover work, continue or start a rebase onto the moved base."""
    workspace, root = Path(lane.workspace), Path(lane.root)
    rebased = []
    for repo in lane.repos:
        work = root if repo.path == "." else root / repo.path
        main = workspace if repo.path == "." else workspace / repo.path
        if not repo.fresh and _rebasing(work):
            result = git(work, *IDENTITY, "rebase", "--continue", check=False)
            if result.returncode:
                return "conflict", _conflicts(repo.path, work)
        if _dirty(work):
            git(work, "add", "-A")
            git(work, *IDENTITY, "commit", "-q", "-m", message)
        if repo.fresh:
            continue
        head = git(main, "rev-parse", "HEAD").stdout.strip()
        if _ancestor(work, head, "HEAD"):
            continue
        result = git(work, *IDENTITY, "rebase", head, check=False)
        if result.returncode:
            return "conflict", _conflicts(repo.path, work)
        rebased.append(f"{repo.path} onto {head[:10]}")
    return "done", (
        "Rebased " + ", ".join(rebased)
    ) if rebased else "Work committed; base unchanged"


def _conflicts(path: str, work: Path) -> str:
    files = git(work, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    return (
        f"Rebase stopped in {path} (lane {work}). Conflicted files: "
        + (", ".join(files[:20]) or "unknown")
        + ". Resolve, `git add` them and run `git rebase --continue`."
    )


def remove_lane(lane: Lane) -> None:
    """Delete worktrees, merged branches and links; never follow a link."""
    workspace, root = Path(lane.workspace), Path(lane.root)
    evidence_dir = root / ENGINE_DIRECTORY
    if evidence_dir.is_dir():
        target = workspace / ENGINE_DIRECTORY
        target.mkdir(exist_ok=True)
        for item in evidence_dir.iterdir():
            if not (target / item.name).exists():
                shutil.move(str(item), str(target / item.name))
    for repo in lane.repos:
        if repo.fresh:
            continue
        main = workspace if repo.path == "." else workspace / repo.path
        work = root if repo.path == "." else root / repo.path
        if work.exists() and not os.path.isjunction(work) and not work.is_symlink():
            git(main, "worktree", "remove", "--force", str(work), check=False)
        git(main, "worktree", "prune", check=False)
        git(main, "branch", "-d", repo.branch, check=False)  # only if merged
    _unlink_tree(root)


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


class LaneHandler:
    """Runs `integrate` or `rebase` for the lane the attempt executes in."""

    def __init__(self, action: str) -> None:
        if action not in ("integrate", "rebase"):
            raise ValueError("Unknown lane action")
        self.action = action
        self.manifest = Manifest("lane-" + action, "0.1.0", capabilities=("process", "operation"))

    def prepare(self, packet: Packet) -> Launch:
        if not (Path(packet.workspace) / LANE_FILE).is_file():
            raise ValueError("This run has no isolated lane; merge steps need one")
        argv = (
            sys.executable,
            "-m",
            "sdd_runtime.lanes",
            self.action,
            packet.directory,
            packet.run_id,
        )
        return Launch(argv, packet.workspace)

    def collect(self, packet: Packet, exit_code: int, revision: str) -> Result:
        path = Path(packet.directory) / RESULT_FILE
        if exit_code != 0 or not path.is_file():
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                f"Lane {self.action} failed (exit {exit_code}); inspect stderr.log",
                revision,
            )
        document = object_json(path.read_text(encoding="utf-8"))
        proof: tuple[Artifact, ...] = (evidence(path, Path(packet.workspace), revision),)
        return Result(
            packet.attempt.id,
            packet.attempt.generation,
            text(document.get("outcome"), "outcome"),
            text(document.get("reason"), "reason"),
            revision,
            proof,
        )


def main() -> None:
    action, directory, run_id = sys.argv[1:4]
    lane = load_lane(Path(LANE_FILE).read_text(encoding="utf-8"))
    if lane.run != run_id:
        raise SystemExit("Lane belongs to another run")
    try:
        if action == "integrate":
            outcome, reason = integrate(lane)
        else:
            outcome, reason = rebase(lane, f"ffai: {run_id} work before merge")
    except (RuntimeError, OSError) as error:
        outcome, reason = "blocked", str(error)
    atomic_write(Path(directory) / RESULT_FILE, json.dumps({"outcome": outcome, "reason": reason}))


if __name__ == "__main__":
    main()
