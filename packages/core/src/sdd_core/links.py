"""Links between repositories of one workspace: which repository pins which, and at what.

A workspace may hold several repositories where one depends on another at a pinned
version (git submodules, path dependencies of a package manager). Tickets change one
repository each; a dependent repository's ticket follows its dependency's ticket and
advances the pin to what that ticket delivered. The rules are pure and shared by every
adapter; adapters (`RepositoryLinks`) only read and write the pins.
"""

import posixpath
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Link:
    """`repository` pins `dependency` at `pinned`, placed at `path` inside it.

    Repository and dependency paths are workspace-relative with forward slashes;
    `path` is relative to the repository.
    """

    path: str
    dependency: str
    pinned: str = ""


class RepositoryLinks(Protocol):
    """Reads and writes the pins of one kind of link (an adapter per mechanism)."""

    def links(self, root: str, repository: str) -> tuple[Link, ...]:
        """The links of `repository` (workspace-relative) in the tree at `root`."""
        ...

    def pin(self, root: str, repository: str, link: Link, commit: str) -> None:
        """Pin `link` of `repository` at `commit` (staged, not committed)."""
        ...

    def add(self, root: str, repository: str, link: Link, commit: str) -> None:
        """Add a new `link` to `repository`, pinned at `commit` (staged, not committed)."""
        ...


def stale(links: tuple[Link, ...], heads: dict[str, str]) -> tuple[tuple[Link, str], ...]:
    """Links whose dependency has a newer delivered head, with that head."""
    return tuple(
        (link, heads[link.dependency])
        for link in links
        if link.dependency in heads and heads[link.dependency] != link.pinned
    )


def missing(links: tuple[Link, ...], heads: dict[str, str], repository: str) -> tuple[str, ...]:
    """Delivered dependencies the repository does not link yet (never itself)."""
    linked = {link.dependency for link in links}
    return tuple(sorted(d for d in heads if d not in linked and d != repository))


def infer_link(existing: tuple[Link, ...], dependency: str) -> Link | None:
    """A new link placed like its neighbours: same folder for dependencies that live in
    the same folder (e.g. `addons/<name>` for `libraries/<name>`), or None."""
    parent, name = posixpath.split(dependency)
    places = {
        posixpath.dirname(link.path)
        for link in existing
        if posixpath.dirname(link.dependency) == parent
        and posixpath.basename(link.path) == posixpath.basename(link.dependency)
    }
    if len(places) != 1:
        return None
    (place,) = places
    return Link(posixpath.join(place, name) if place else name, dependency)
