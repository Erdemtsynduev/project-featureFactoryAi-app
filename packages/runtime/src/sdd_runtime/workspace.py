"""Workspace IO boundary used by application use cases.

A run executes in one workspace folder. Its claim is the set of paths it owns:
by default the whole workspace, or explicit sub-repositories of a multi-repo
workspace. Claims decide both resource ownership and the observed revision.
"""

import json
from dataclasses import replace
from pathlib import Path

from sdd_core.codec import canonical
from sdd_core.evidence import durable_evidence
from sdd_core.models import Result

from sdd_runtime.files import ENGINE_DIRECTORY, verify_evidence


def claim_paths(claim: str) -> tuple[str, ...]:
    """A claim is one absolute path or a canonical JSON array of absolute paths."""
    if not claim.startswith("["):
        return (claim,)
    value = json.loads(claim)
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError("Invalid workspace claim")
    return tuple(value)


def scoped_claim(root: str, scope: tuple[str, ...]) -> str:
    """Resolve relative scope paths to a claim that stays inside the workspace."""
    base = Path(root).resolve(strict=True)
    if not scope or scope == (".",):
        return str(base)
    paths = []
    for item in scope:
        if Path(item).is_absolute():
            raise ValueError("Scope paths are relative to the workspace")
        # A scoped repository may be created by the run itself; its parent must exist.
        path = (base / item).resolve()
        parent = path.parent.resolve(strict=True)
        if (
            path == base
            or not parent.is_relative_to(base)
            or not path.is_relative_to(base)
            or (path.exists() and (path.is_symlink() or not path.is_dir()))
        ):
            raise ValueError(f"Scope must be a folder inside the workspace: {item}")
        paths.append(str(path))
    unique = sorted(set(paths))
    if any(a != b and overlaps(a, b) for a in unique for b in unique):
        raise ValueError("Scope paths must not contain each other")
    return unique[0] if len(unique) == 1 else canonical(unique)


def overlaps(left: str, right: str) -> bool:
    for one in claim_paths(left):
        for other in claim_paths(right):
            a, b = Path(one).resolve(), Path(other).resolve()
            if a == b or a in b.parents or b in a.parents:
                return True
    return False


class LocalWorkspace:
    def resolve(self, path: str) -> str:
        return str(Path(path).resolve(strict=True))

    def claim(self, root: str, scope: tuple[str, ...]) -> str:
        return scoped_claim(root, scope)

    def paths(self, claim: str) -> tuple[str, ...]:
        return claim_paths(claim)

    def overlaps(self, left: str, right: str) -> bool:
        return overlaps(left, right)

    def normalize(self, run_id: str, result: Result, workspace: str) -> Result:
        root = Path(workspace).resolve()
        artifacts = []
        for artifact in result.artifacts:
            path = (root / artifact.path).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Evidence escapes workspace")
            artifacts.append(replace(artifact, path=path.relative_to(root).as_posix()))
        checkpoint = f"{ENGINE_DIRECTORY}/{run_id}/checkpoint.md"
        return replace(result, artifacts=durable_evidence(tuple(artifacts), checkpoint))

    def verify(self, result: Result, workspace: str, revision: str) -> None:
        verify_evidence(result.artifacts, Path(workspace), revision)
