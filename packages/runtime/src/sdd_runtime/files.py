"""Durable files and revision-bound evidence at the IO boundary."""

import hashlib
import os
import subprocess
import time
from pathlib import Path, PurePosixPath

from sdd_core import revision as revisions
from sdd_core.models import Artifact
from sdd_core.sdk import ENGINE_DIRECTORY, evidence_name

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


# Folders of a workspace that are not its content.
SCRATCH = (".git", "__pycache__", ".venv", ".pytest_cache")


def work_folder(database: Path) -> Path:
    """The engine's own folder beside its database: attempt files and lanes live here,
    so a project receives nothing but its code."""
    return database.with_name(database.stem + ".work")


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
        return revisions.named(checksum.hexdigest())
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
    return revisions.named(checksum.hexdigest())


def _checksum(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def evidence(path: Path, workspace: Path, folder: Path, current_revision: str) -> Artifact:
    """A file as evidence of the attempt whose folder is `folder` (see `evidence_name`)."""
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("Evidence must be a file")
    name = evidence_name(
        resolved.as_posix(), workspace.resolve().as_posix(), folder.resolve().as_posix()
    )
    return Artifact(name, _checksum(resolved), current_revision)


def locate(name: str, workspace: Path, attempts: Path) -> Path:
    """The file an evidence name means: an attempt's file under `attempts` (the engine's
    folder of attempt files), else a file of the workspace."""
    first, *rest = PurePosixPath(name).parts or ("",)
    root = attempts if first == ENGINE_DIRECTORY else workspace
    path = (root.joinpath(*rest) if first == ENGINE_DIRECTORY else root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Evidence escapes workspace")
    return path


def verify_evidence(
    artifacts: tuple[Artifact, ...], workspace: Path, attempts: Path, current_revision: str
) -> None:
    for artifact in artifacts:
        path = locate(artifact.path, workspace, attempts)
        if not path.is_file() or artifact != Artifact(
            artifact.path, _checksum(path), current_revision
        ):
            raise ValueError("Stale or modified evidence")
