"""Evidence policy over identities already canonicalized by a workspace adapter."""

from sdd_core.models import Artifact


def durable_evidence(artifacts: tuple[Artifact, ...], checkpoint: str) -> tuple[Artifact, ...]:
    result = tuple(artifact for artifact in artifacts if artifact.path != checkpoint)
    if not result:
        raise ValueError("Acceptance requires evidence other than the mutable checkpoint")
    if len({artifact.path for artifact in result}) != len(result):
        raise ValueError("Duplicate evidence identity")
    return result
