"""A project's settings as the factory uses them, read once from its stored document.

`ProjectCatalog.save_project` validates what a person enters and stores it; flows,
checks and commits read the typed settings here instead of the raw document.
"""

from dataclasses import dataclass, field

from sdd_core.codec import mapping, sequence, text
from sdd_core.models import Json
from sdd_workflows.templates import CheckCommand


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
