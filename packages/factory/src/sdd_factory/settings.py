"""A project's settings as the factory uses them, read once from its stored document.

`ProjectCatalog.save_project` validates what a person enters and stores it; flows,
checks and commits read the typed settings here instead of the raw document.
"""

import re
from dataclasses import dataclass, field, fields

from sdd_core.codec import mapping, sequence, text
from sdd_core.models import Json
from sdd_workflows.templates import DEFAULT_ROLES, CheckCommand, Roles

ROLES = tuple(item.name for item in fields(Roles))
# The profile each role uses unless the project names another, in the roles' own order.
ROLE_DEFAULTS: list[Json] = [[role, getattr(DEFAULT_ROLES, role)] for role in ROLES]
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def roles_of(raw: Json) -> dict[str, Json]:
    """A project's roles as stored: the profile of each role a person chose. A role
    left out keeps the template's default profile."""
    chosen = {role: text(name, "profile").strip() for role, name in mapping(raw or {}).items()}
    if unknown := sorted(set(chosen) - set(ROLES)):
        raise ValueError(f"Unknown roles: {', '.join(unknown)}; known: {', '.join(ROLES)}")
    if invalid := sorted(name for name in chosen.values() if name and not PROFILE.fullmatch(name)):
        raise ValueError(f"Invalid profile names: {', '.join(invalid)}")
    return {role: name for role, name in sorted(chosen.items()) if name}


@dataclass(frozen=True)
class ProjectSettings:
    checks: tuple[str, ...] = ()
    # Checks of each repository (workspace-relative): a ticket runs those it owns.
    repository_checks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    isolation: bool = True
    auto_resolve: bool = True
    web: bool = False
    commit_message: str = ""
    pin_message: str = ""
    # Which agent profile plays each role in the project's flows.
    roles: Roles = DEFAULT_ROLES

    @classmethod
    def of(cls, project: dict[str, Json]) -> "ProjectSettings":
        return cls(
            tuple(text(x, "check") for x in sequence(project.get("checks", []))),
            {
                repository: tuple(text(x, "check") for x in sequence(argv))
                for repository, argv in mapping(project.get("repository_checks", {})).items()
            },
            project.get("isolation", True) is not False,
            project.get("auto_resolve", True) is not False,
            project.get("web") is True,
            str(project.get("commit_message") or ""),
            str(project.get("pin_message") or ""),
            Roles(**{role: str(name) for role, name in roles_of(project.get("roles")).items()}),
        )

    def ticket_checks(self, repositories: tuple[str, ...]) -> tuple[CheckCommand, ...]:
        """The checks a ticket runs: those of the repositories it owns, else the
        project's own checks, else none."""
        owned = [
            CheckCommand(
                self.repository_checks[repository], repository, title=f"checks · {repository}"
            )
            for repository in repositories
            if self.repository_checks.get(repository)
        ]
        if owned:
            return tuple(owned)
        return (CheckCommand(self.checks, title="checks"),) if self.checks else ()
