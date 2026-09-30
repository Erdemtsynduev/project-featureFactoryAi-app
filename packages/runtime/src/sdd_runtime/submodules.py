"""Repository links as git submodules: the one place that knows `.gitmodules`.

A submodule whose url resolves to another repository of the workspace (a relative
url such as `../libraries/sky`) is a link to that repository. Pins are gitlinks in
the index; the caller commits them. Submodules pointing outside the workspace are
not links: they keep whatever the repository pins.
"""

import os
import posixpath
from pathlib import Path

from sdd_core.links import Link, RepositoryLinks

from sdd_runtime.git import git

GITMODULES = ".gitmodules"
GITLINK = "160000"


def _relative(path: str) -> str:
    return path.replace("\\", "/").strip("/") or "."


class GitSubmodules:
    """`RepositoryLinks` for repositories that pin their dependencies as submodules."""

    @staticmethod
    def applies(root: str, repository: str) -> bool:
        return (Path(root) / repository / GITMODULES).is_file()

    def links(self, root: str, repository: str) -> tuple[Link, ...]:
        work = Path(root) / repository
        if not (work / GITMODULES).is_file():
            return ()
        listed = git(work, "config", "-f", GITMODULES, "--list", check=False).stdout
        paths: dict[str, str] = {}
        urls: dict[str, str] = {}
        for line in listed.splitlines():
            key, _, value = line.partition("=")
            section, _, field = key.rpartition(".")
            if not section.startswith("submodule."):
                continue
            name = section.removeprefix("submodule.")
            (paths if field == "path" else urls if field == "url" else {})[name] = value
        found: list[Link] = []
        for name, path in sorted(paths.items()):
            dependency = self._dependency(repository, urls.get(name, ""))
            if dependency is not None:
                found.append(Link(_relative(path), dependency, self._pinned(work, path)))
        return tuple(found)

    def pin(self, root: str, repository: str, link: Link, commit: str) -> None:
        git(
            Path(root) / repository,
            "update-index",
            "--cacheinfo",
            f"{GITLINK},{commit},{link.path}",
        )

    def add(self, root: str, repository: str, link: Link, commit: str) -> None:
        work = Path(root) / repository
        url = posixpath.relpath(link.dependency, _relative(repository))
        section = f"submodule.{link.path}"
        git(work, "config", "-f", GITMODULES, f"{section}.path", link.path)
        git(work, "config", "-f", GITMODULES, f"{section}.url", url)
        git(work, "add", GITMODULES)
        git(work, "update-index", "--add", "--cacheinfo", f"{GITLINK},{commit},{link.path}")

    @staticmethod
    def _dependency(repository: str, url: str) -> str | None:
        """The workspace-relative repository a relative url names, else None."""
        if not url.startswith(("./", "../")):
            return None
        joined = posixpath.normpath(posixpath.join(_relative(repository), url))
        return None if joined.startswith("..") or joined == "." else joined

    @staticmethod
    def _pinned(work: Path, path: str) -> str:
        staged = git(work, "ls-files", "--stage", "--", path, check=False).stdout.split()
        return staged[1] if len(staged) >= 2 and staged[0] == GITLINK else ""


class NoLinks:
    """A repository that pins no other repository of the workspace."""

    @staticmethod
    def applies(root: str, repository: str) -> bool:
        return True

    def links(self, root: str, repository: str) -> tuple[Link, ...]:
        return ()

    def pin(self, root: str, repository: str, link: Link, commit: str) -> None:
        raise ValueError(f"{repository} has no repository links")

    def add(self, root: str, repository: str, link: Link, commit: str) -> None:
        raise ValueError(f"{repository} has no repository links to add to")


# Link mechanisms in the order they are tried; the first that applies owns a repository.
ADAPTERS = (GitSubmodules(), NoLinks())


def links_of(root: str, repository: str) -> RepositoryLinks:
    """The link adapter for one repository, chosen by what the repository contains."""
    return next(adapter for adapter in ADAPTERS if adapter.applies(root, repository))


def workspace_relative(workspace: str, path: str) -> str:
    """A path inside the workspace in the forward-slash form links use."""
    return _relative(os.path.relpath(path, workspace))
