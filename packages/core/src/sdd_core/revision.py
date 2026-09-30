"""Revisions: the name of the content a run owns, in one format.

A revision is "<format>:<digest>". Adapters only hash bytes; how a digest becomes a
revision, how several owned paths combine and what a not-yet-created path is called
are decided here. A revision recorded in an earlier format names the same content
differently, so it is re-based, never mistaken for an outside change.
"""

import hashlib
from collections.abc import Iterable

from sdd_core.wire import canonical, digest

FORMAT = "c1"


def named(content_digest: str) -> str:
    return f"{FORMAT}:{content_digest}"


def earlier_format(revision: str) -> bool:
    return not revision.startswith(FORMAT + ":")


def absent(name: str) -> str:
    """A scoped path the run has not created yet."""
    return named(hashlib.sha256(b"absent:" + name.encode()).hexdigest())


def composite(parts: Iterable[tuple[str, str]]) -> str:
    """One revision for a run that owns several paths: each path with its revision."""
    return named(digest(canonical([[path, revision] for path, revision in parts])))
