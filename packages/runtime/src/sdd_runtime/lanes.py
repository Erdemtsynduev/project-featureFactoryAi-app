"""Isolated working copies ("lanes"): one git worktree per task, as in Orca or a
worktree-per-task factory.

A lane lives at `<workspace>/.sdd-lanes/<run>/`. Every repository in the run's
scope is a git worktree on branch `ffai/<run>`, created from the repository's
current HEAD when the run first starts. Everything else in the workspace is
linked (a junction on Windows, a symlink elsewhere) so tools and project checks
see the same layout; linked folders are other checkouts and must not be edited.

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
After acceptance the coordinator removes worktrees, branches and links; evidence
is moved back into the workspace so history stays inspectable.
"""

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sdd_core.codec import canonical, decode, encode, object_json, text
from sdd_core.links import Link, infer_link, missing, stale
from sdd_core.models import Artifact, Result
from sdd_core.options import StepOptions
from sdd_core.sdk import PACKET_FILE, Launch, Manifest, Packet
from sdd_core.tickets import ticket_title

from sdd_runtime.files import ENGINE_DIRECTORY, LANES, atomic_write, evidence
from sdd_runtime.git import git, is_repository_top
from sdd_runtime.submodules import links_of


def lane_branch(run_id: str) -> str:
    """The branch a run's lane works on in every repository of its scope."""
    return "ffai/" + run_id


LANE_FILE = ".sdd-lane.json"
RESULT_FILE = "lane-result.json"
SKIP = {".git", LANES, ENGINE_DIRECTORY}
# Commits made on behalf of the engine need an identity even on fresh machines.
IDENTITY = ("-c", "user.name=Feature Factory", "-c", "user.email=feature-factory@localhost")
# Version 2 lanes check linked dependencies out (`attached`).
LANE_VERSION = 2
# Subjects longer than this are cut: commit tools and reviewers expect short subjects.
SUBJECT_CHARS = 72


@dataclass(frozen=True)
class CommitMessages:
    """A project's commit convention for engine commits; Conventional without attribution
    by default. `{repo}` is the repository's folder name."""

    work: str = "feat({repo}): {title}"
    pin: str = "build({repo}): pin {dependency} {sha}"

    @classmethod
    def of(cls, options: StepOptions) -> "CommitMessages":
        defaults = cls()
        return cls(options.commit_message or defaults.work, options.pin_message or defaults.pin)

    def check(self) -> None:
        """Refuse a template that names an unknown field."""
        self.for_work("repo", "title")
        self.for_pin("repo", "dependency", "0" * 40)

    def for_work(self, repo: str, title: str) -> str:
        return _subject(self.work.format(repo=repo, title=title))

    def for_pin(self, repo: str, dependency: str, sha: str) -> str:
        name = dependency.rsplit("/", 1)[-1]
        return _subject(self.pin.format(repo=repo, dependency=name, sha=sha[:10]))


def _subject(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= SUBJECT_CHARS else line[: SUBJECT_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class LaneJob:
    """What a lane action needs from its attempt: the ticket, the convention, and the
    repositories its accepted prerequisites delivered."""

    title: str
    messages: CommitMessages = CommitMessages()
    delivered: tuple[str, ...] = ()


@dataclass
class LaneRepo:
    path: str  # relative to the workspace
    branch: str
    base: str  # commit the lane started from ("" for a repository the task creates)
    origin: str  # branch checked out in the main copy at creation ("" if detached)
    fresh: bool = False


@dataclass
class Attached:
    """A dependency checked out inside the lane where a repository links it."""

    path: str  # relative to the lane root
    dependency: str  # the dependency repository, relative to the workspace


@dataclass
class Lane:
    run: str
    workspace: str
    root: str
    repos: list[LaneRepo] = field(default_factory=list)
    status: str = "active"  # opening | active | removing | removed
    version: int = LANE_VERSION
    attached: list[Attached] = field(default_factory=list)

    def document(self) -> str:
        return canonical(encode(self))

    def work(self, repo: LaneRepo) -> Path:
        """The repository's working copy in the lane."""
        return Path(self.root) if repo.path == "." else Path(self.root) / repo.path

    def main(self, repo: LaneRepo) -> Path:
        """The repository's main copy in the workspace."""
        return Path(self.workspace) if repo.path == "." else Path(self.workspace) / repo.path


def load_lane(document: str) -> Lane:
    raw = object_json(document)
    # A lane written before versions existed is version 1: it has no attached links yet.
    return decode(Lane, {"version": 1, **raw})


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
    atomic_write(root / LANE_FILE, lane.document())
    for repo in repos:
        _exclude(lane.work(repo), (LANE_FILE, ENGINE_DIRECTORY + "/"))
    if is_repository_top(workspace):
        _exclude(workspace, (LANES + "/",))
    attach_links(lane)
    atomic_write(root / LANE_FILE, lane.document())
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


# The lane actions a workflow step can run, by name.
ACTIONS: dict[str, Callable[[Lane, LaneJob], tuple[str, str]]] = {
    "commit": commit,
    "integrate": lambda lane, job: integrate(lane),
    "rebase": rebase,
}


def _conflicts(path: str, work: Path) -> str:
    files = git(work, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    return (
        f"Rebase stopped in {path} (lane {work}). Conflicted files: "
        + (", ".join(files[:20]) or "unknown")
        + ". Resolve, `git add` them and run `git rebase --continue`."
    )


def remove_lane(lane: Lane) -> None:
    """Delete worktrees, merged branches and links; never follow a link."""
    written = Path(lane.root) / LANE_FILE
    if written.is_file():
        lane = load_lane(written.read_text(encoding="utf-8"))  # the lane's latest record
    workspace, root = Path(lane.workspace), Path(lane.root)
    evidence_dir = root / ENGINE_DIRECTORY
    if evidence_dir.is_dir():
        target = workspace / ENGINE_DIRECTORY
        target.mkdir(exist_ok=True)
        for item in evidence_dir.iterdir():
            if not (target / item.name).exists():
                shutil.move(str(item), str(target / item.name))
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
