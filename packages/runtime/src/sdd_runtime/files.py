"""Durable files and revision-bound evidence at the IO boundary."""

import hashlib
import os
import subprocess
import time
from pathlib import Path

from sdd_core.models import Artifact

from sdd_runtime.platform import NO_WINDOW


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.02 * (attempt + 1))


# Engine scratch inside a workspace: packets, host receipts and logs per attempt.
ENGINE_DIRECTORY = ".sdd-engine"
SCRATCH = (".git", ENGINE_DIRECTORY, ".sdd-lanes", "__pycache__", ".venv", ".pytest_cache")


def attempt_folder(workspace: Path, run_id: str, attempt_id: str) -> Path:
    return workspace / ENGINE_DIRECTORY / run_id / attempt_id


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        capture_output=True,
        check=check,
        timeout=60,
        creationflags=NO_WINDOW,
    )


def _changed_paths(root: Path, top: Path) -> set[Path]:
    """Paths under `root` whose content differs from HEAD: modified, deleted, renamed
    or untracked.

    Unchanged tracked files are identified by the HEAD tree itself, so large
    repositories are not re-read on every observation. Ignored files stay out.
    A nested repository appears as one untracked directory, never recursively.
    Porcelain paths are relative to the repository top, even for a subfolder.
    """
    listing = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
    paths: set[Path] = set()
    entries = iter(listing.stdout.split(b"\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.add(top / os.fsdecode(entry[3:]))
        if entry[:1] in (b"R", b"C"):
            paths.add(top / os.fsdecode(next(entries, b"")))
    return {path for path in paths if path == root or path.is_relative_to(root)}


def revision(root: Path) -> str:
    """Git HEAD plus the content of every change against it, excluding engine scratch.

    A repository top hashes its HEAD commit; a folder inside a repository hashes
    only its own HEAD tree, so unrelated commits elsewhere do not change it.
    Non-Git workspaces intentionally use a full content snapshot.
    """
    root = root.resolve(strict=True)
    response = _git(root, "rev-parse", "--show-toplevel", "HEAD", check=False)
    lines = response.stdout.decode(errors="replace").split()
    if response.returncode == 0 and len(lines) == 2:
        top = Path(lines[0]).resolve()
        if top == root:
            checksum = hashlib.sha256(b"git-head:" + lines[1].encode())
        else:
            tree = _git(root, "rev-parse", "HEAD:./", check=False).stdout.strip()
            checksum = hashlib.sha256(b"git-tree:" + tree)
        paths = _changed_paths(root, top)
    else:
        checksum = hashlib.sha256(b"no-git")
        paths = set(root.rglob("*"))
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(p in SCRATCH for p in relative.parts):
            continue
        if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(root)):
            raise ValueError("Revision cannot silently follow workspace links")
        checksum.update(relative.as_posix().encode())
        if path.is_file():
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    checksum.update(block)
        elif not path.exists():
            checksum.update(b"<deleted>")
    return checksum.hexdigest()


def evidence(path: Path, root: Path, current_revision: str) -> Artifact:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Evidence must be a workspace file")
    checksum = hashlib.sha256()
    with resolved.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    return Artifact(
        resolved.relative_to(root.resolve()).as_posix(), checksum.hexdigest(), current_revision
    )


def verify_evidence(artifacts: tuple[Artifact, ...], root: Path, current_revision: str) -> None:
    for artifact in artifacts:
        if evidence(root / artifact.path, root, current_revision) != artifact:
            raise ValueError("Stale or modified evidence")
