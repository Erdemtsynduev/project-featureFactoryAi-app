"""Rules every storage backend enforces the same way; backends only fetch rows.

Each function decides from values the backend read; a backend never re-implements
one of these rules in its own query language.
"""

from collections.abc import Callable, Iterable


def check_dependencies(
    run_id: str, dependencies: Iterable[str], exists: Callable[[str], bool]
) -> tuple[str, ...]:
    """A new run's prerequisites, sorted and unique: each an existing other run."""
    wanted = tuple(sorted(set(dependencies)))
    if run_id in wanted or not all(exists(key) for key in wanted):
        raise ValueError("Invalid dependencies")
    return wanted
