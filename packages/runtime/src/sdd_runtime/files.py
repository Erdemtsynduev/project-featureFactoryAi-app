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


def revision(root: Path) -> str:
    """Git HEAD plus tracked/untracked contents, excluding engine scratch only.

    Non-Git workspaces intentionally use a content snapshot as well.
    """
    root = root.resolve(strict=True)
    response = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        timeout=15,
        creationflags=NO_WINDOW,
    )
    checksum = hashlib.sha256(response.stdout if response.returncode == 0 else b"no-git")
    if response.returncode == 0:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        paths = {root / os.fsdecode(item) for item in listing.stdout.split(b"\0") if item}
    else:
        paths = set(root.rglob("*"))
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(
            p in (".git", ".sdd-engine", "__pycache__", ".venv", ".pytest_cache")
            for p in relative.parts
        ):
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
