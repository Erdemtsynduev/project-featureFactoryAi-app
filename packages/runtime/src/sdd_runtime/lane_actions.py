"""Lane steps: commit the agent's work and pins, rebase onto a moved base, integrate.

Committing is a workflow step too: `commit` records the agent's work with the project's
commit message and advances the pins of dependencies the run's accepted prerequisites
delivered (see `sdd_core.links`), so checks and review always see a committed,
consistently pinned repository.

Merging back is a workflow step, never implicit:
* `integrate` (read-only for the lane) fast-forwards each main checkout to the
  lane branch, or reports `behind` when the lane has uncommitted work or the
  base branch moved;
* `rebase` (mutating) commits leftover work and rebases onto the moved base,
  continuing an interrupted rebase, and reports `conflict` with the files when
  git stops. An agent (autonomous mode) or a human then resolves it in the lane.
After acceptance the coordinator removes the lane (`lanes.remove_lane`).
"""

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import decode, object_json, text
from sdd_core.links import Link, infer_link, missing, stale
from sdd_core.models import Artifact, Result
from sdd_core.sdk import PACKET_FILE, Launch, Manifest, Packet
from sdd_core.tickets import ticket_title

from sdd_runtime.files import atomic_write, evidence
from sdd_runtime.git import git, is_repository_top
from sdd_runtime.lane_model import (
    LANE_FILE,
    RESULT_FILE,
    Attached,
    CommitMessages,
    Lane,
    LaneJob,
    LaneRepo,
    load_lane,
)
from sdd_runtime.lanes import link_folder
from sdd_runtime.submodules import links_of

# Commits made on behalf of the engine need an identity even on fresh machines.
IDENTITY = ("-c", "user.name=Feature Factory", "-c", "user.email=feature-factory@localhost")


def _dirty(repo: Path) -> bool:
    status = git(
        repo, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=dirty"
    )
    return bool(status.stdout.strip())


def _author(repo: Path) -> tuple[str, ...]:
    """The repository's configured author; the engine's identity only when there is none."""
    configured = git(repo, "config", "--get", "user.email", check=False).stdout.strip()
    return () if configured else IDENTITY


def _commit(repo: Path, message: str, *, everything: bool = True) -> None:
    if everything:
        git(repo, "add", "-A")
    git(repo, *_author(repo), "commit", "-q", "-m", message)


def _name(lane: Lane, repo: LaneRepo) -> str:
    return Path(lane.workspace if repo.path == "." else repo.path).name


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _rebasing(repo: Path) -> bool:
    admin = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    return (admin / "rebase-merge").exists() or (admin / "rebase-apply").exists()


def integrate(lane: Lane) -> tuple[str, str]:
    """Fast-forward main checkouts; `behind` when a rebase or commit must come first."""
    plans = []
    for repo in lane.repos:
        work, main = lane.work(repo), lane.main(repo)
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
            link_folder(main, work)
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


def rebase(lane: Lane, job: LaneJob) -> tuple[str, str]:
    """Commit leftover work, continue or start a rebase onto the moved base."""
    rebased = []
    for repo in lane.repos:
        work, main = lane.work(repo), lane.main(repo)
        if not repo.fresh and _rebasing(work):
            result = git(work, *_author(work), "rebase", "--continue", check=False)
            if result.returncode:
                return "conflict", _conflicts(repo.path, work)
        if _dirty(work):
            _commit(work, job.messages.for_work(_name(lane, repo), job.title))
        if repo.fresh:
            continue
        head = git(main, "rev-parse", "HEAD").stdout.strip()
        if _ancestor(work, head, "HEAD"):
            continue
        result = git(work, *_author(work), "rebase", head, check=False)
        if result.returncode:
            return "conflict", _conflicts(repo.path, work)
        rebased.append(f"{repo.path} onto {head[:10]}")
    return "done", (
        "Rebased " + ", ".join(rebased)
    ) if rebased else "Work committed; base unchanged"


def commit(lane: Lane, job: LaneJob) -> tuple[str, str]:
    """Commit the agent's work, then advance the pins of delivered dependencies.

    One commit per pinned dependency, dependencies the repository does not link yet are
    linked like its neighbours; without a pattern to follow the step is blocked. Nothing
    to do is `done` too, so the step can run after every agent pass.
    """
    workspace, root = Path(lane.workspace), Path(lane.root)
    heads = {
        dependency: git(workspace / dependency, "rev-parse", "HEAD").stdout.strip()
        for dependency in job.delivered
        if is_repository_top(workspace / dependency)
    }
    made: list[str] = []
    for repo in lane.repos:
        work, name = lane.work(repo), _name(lane, repo)
        if _dirty(work):
            _commit(work, job.messages.for_work(name, job.title))
            made.append(f"{repo.path}: work")
        if repo.fresh or not heads:
            continue
        adapter = links_of(str(root), repo.path)
        links = adapter.links(str(root), repo.path)
        if not links:
            continue  # pins nothing of this workspace: the work commit is all
        for link, head in stale(links, heads):
            if link.pinned and not _ancestor(workspace / link.dependency, link.pinned, head):
                continue  # never move a pin backwards
            adapter.pin(str(root), repo.path, link, head)
            _check_out(lane, repo, link, head)
            _commit(work, job.messages.for_pin(name, link.dependency, head), everything=False)
            made.append(f"{repo.path}: pin {link.dependency} {head[:10]}")
        for dependency in missing(links, heads, repo.path):
            new = infer_link(links, dependency)
            if new is None:
                return "blocked", (
                    f"{repo.path} does not link {dependency} and has no neighbouring link "
                    "to follow; add the link by hand, then retry"
                )
            adapter.add(str(root), repo.path, new, heads[dependency])
            _check_out(lane, repo, new, heads[dependency])
            _commit(
                work,
                job.messages.for_pin(name, dependency, heads[dependency]),
                everything=False,
            )
            made.append(f"{repo.path}: link {dependency} {heads[dependency][:10]}")
    return "done", ("Committed " + "; ".join(made)) if made else "Nothing to commit"


def _check_out(lane: Lane, repo: LaneRepo, link: Link, commit: str) -> None:
    """Show the dependency at its new pin where the repository links it."""
    place = link.path if repo.path == "." else f"{repo.path}/{link.path}"
    target, source = Path(lane.root) / place, Path(lane.workspace) / link.dependency
    if any(item.path == place for item in lane.attached):
        git(target, "checkout", "-q", "--detach", commit)
        return
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
    if not target.exists():
        git(source, "worktree", "add", "--detach", str(target), commit)
        lane.attached.append(Attached(place, link.dependency))


def _conflicts(path: str, work: Path) -> str:
    files = git(work, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    return (
        f"Rebase stopped in {path} (lane {work}). Conflicted files: "
        + (", ".join(files[:20]) or "unknown")
        + ". Resolve, `git add` them and run `git rebase --continue`."
    )


# The lane actions a workflow step can run, by name.
ACTIONS: dict[str, Callable[[Lane, LaneJob], tuple[str, str]]] = {
    "commit": commit,
    "integrate": lambda lane, job: integrate(lane),
    "rebase": rebase,
}


class LaneHandler:
    """Runs one lane action (see `ACTIONS`) for the lane the attempt executes in."""

    def __init__(self, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError("Unknown lane action")
        self.action = action
        self.manifest = Manifest("lane-" + action, "0.1.0", capabilities=("process", "operation"))

    def prepare(self, packet: Packet) -> Launch:
        if not (Path(packet.workspace) / LANE_FILE).is_file():
            raise ValueError("This run has no isolated lane; merge steps need one")
        argv = (
            sys.executable,
            "-m",
            "sdd_runtime.lane_actions",
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


def job_of(directory: Path, run_id: str) -> LaneJob:
    """The lane job an attempt describes in its packet (written before launch)."""
    packet = decode(Packet, object_json((directory / PACKET_FILE).read_text(encoding="utf-8")))
    return LaneJob(
        ticket_title(packet.context) or run_id,
        CommitMessages.of(packet.step.options),
        packet.delivered,
    )


def main() -> None:
    action, directory, run_id = sys.argv[1:4]
    lane = load_lane(Path(LANE_FILE).read_text(encoding="utf-8"))
    if lane.run != run_id:
        raise SystemExit("Lane belongs to another run")
    try:
        outcome, reason = ACTIONS[action](lane, job_of(Path(directory), run_id))
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        outcome, reason = "blocked", str(error)
    # Links a commit added belong to the lane record, so removing the lane finds them.
    atomic_write(Path(lane.root) / LANE_FILE, lane.document())
    atomic_write(Path(directory) / RESULT_FILE, json.dumps({"outcome": outcome, "reason": reason}))


if __name__ == "__main__":
    main()
