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


def _entries(output: bytes) -> list[bytes]:
    return [entry for entry in output.split(b"\0") if entry]


def _content_ids(root: Path, top: Path) -> dict[str, str]:
    """Each file under `root` (relative to the repository top) and the id of its
    content: the index's blob id when the working file matches it, else the id Git
    gives the working content (`hash-object`, with the repository's filters), or
    `<deleted>`. Committing or staging moves no id, so neither changes a revision.

    Unchanged files are named by the index, so large repositories are not re-read on
    every observation. Ignored files stay out; a nested repository that is not
    ignored is one untracked directory, never recursive.
    """
    ids: dict[str, str] = {}
    for entry in _entries(_git(root, "ls-files", "-s", "-z", "--full-name", "--", ".").stdout):
        meta, listed = entry.split(b"\t", 1)
        ids[os.fsdecode(listed)] = meta.split()[1].decode()
    changed = _entries(_git(root, "diff", "--name-only", "-z", "--", ".").stdout)
    untracked = _entries(
        _git(root, "ls-files", "-o", "-z", "--full-name", "--exclude-standard", "--", ".").stdout
    )
    hashed: list[str] = []
    for raw in (*changed, *untracked):
        name = os.fsdecode(raw).rstrip("/")
        path = top / name
        if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(top)):
            raise ValueError("Revision cannot silently follow workspace links")
        if path.is_file():
            hashed.append(name)
        else:
            ids[name] = "<deleted>" if not path.exists() else "<directory>"
    if hashed:
        listing = "\n".join(hashed).encode()
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(top), "hash-object", "--stdin-paths"],
            input=listing,
            capture_output=True,
            check=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
        ids.update(zip(hashed, result.stdout.decode().split(), strict=True))
    return ids


def revision(root: Path) -> str:
    """The content of `root`, excluding engine scratch.

    Inside a Git repository it names every file by the id of its current content, so a
    commit or `git add` that changes no content keeps the revision; a folder sees only
    its own files, so changes elsewhere in the repository do not move it. Non-Git
    workspaces intentionally use a full content snapshot.
    """
    root = root.resolve(strict=True)
    response = _git(root, "rev-parse", "--show-toplevel", check=False)
    if response.returncode == 0:
        top = Path(response.stdout.decode(errors="replace").strip()).resolve()
        checksum = hashlib.sha256(b"git-content:")
        for name, content in sorted(_content_ids(root, top).items()):
            if any(part in SCRATCH for part in Path(name).parts):
                continue
            checksum.update(f"{name}\0{content}\0".encode())
        return checksum.hexdigest()
    checksum = hashlib.sha256(b"no-git")
    for path in sorted(root.rglob("*")):
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
