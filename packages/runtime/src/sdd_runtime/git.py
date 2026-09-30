"""Explicit Git operations. Integration is journaled before touching the target."""

import os
import subprocess
from pathlib import Path

from sdd_core import revision as revisions
from sdd_core.codec import canonical, object_json, text
from sdd_core.sdk import Manifest

from sdd_runtime.files import atomic_write, revision
from sdd_runtime.platform import NO_WINDOW


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


class GitProject:
    manifest = Manifest("git", "0.1.0", capabilities=("revision", "worktree", "integration"))

    def revision(self, workspace: str) -> str:
        path = Path(workspace)
        if not path.exists() and path.parent.is_dir():
            # A scoped repository that the run has not created yet.
            return revisions.absent(path.name)
        return revision(path)

    @staticmethod
    def command(repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo.resolve()), *args],
            capture_output=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout.decode(errors="replace").strip()

    def create_worktree(self, repo: Path, target: Path, base: str) -> str:
        if target.exists():
            raise FileExistsError(target)
        sha = self.command(repo, "rev-parse", "--verify", base + "^{commit}")
        self.command(repo, "worktree", "add", "--detach", str(target.resolve()), sha)
        return sha

    def integrate(self, repo: Path, candidate: str, expected_base: str, journal: Path) -> str:
        """Fast-forward only. A conflict is an explicit product blocker, never auto-reset."""
        target = self.command(repo, "rev-parse", "--verify", candidate + "^{commit}")
        current = self.command(repo, "rev-parse", "HEAD")
        request = {"repo": str(repo.resolve()), "base": expected_base, "target": target}
        if journal.exists():
            saved = object_json(journal.read_text(encoding="utf-8"))
            if saved != request:
                raise ValueError("Integration request id reused")
            if current == text(saved["target"], "target"):
                return current
        else:
            atomic_write(journal, canonical(request))
        if current != expected_base or self.command(repo, "status", "--porcelain"):
            raise ValueError("Integration target changed or is dirty")
        self.command(repo, "merge", "--ff-only", "--no-edit", target)
        return self.command(repo, "rev-parse", "HEAD")
