"""Workspace IO boundary used by application use cases."""

from dataclasses import replace
from pathlib import Path

from sdd_core.evidence import durable_evidence
from sdd_core.models import Result

from sdd_runtime.files import verify_evidence


def overlaps(left: str, right: str) -> bool:
    a, b = Path(left).resolve(), Path(right).resolve()
    return a == b or a in b.parents or b in a.parents


class LocalWorkspace:
    def resolve(self, path: str) -> str:
        return str(Path(path).resolve(strict=True))

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
        checkpoint = f".sdd-engine/{run_id}/checkpoint.md"
        return replace(result, artifacts=durable_evidence(tuple(artifacts), checkpoint))

    def verify(self, result: Result, workspace: str, revision: str) -> None:
        verify_evidence(result.artifacts, Path(workspace), revision)
