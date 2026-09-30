"""Rules every storage backend enforces the same way; backends only fetch rows.

Each function decides from values the backend read; a backend never re-implements
one of these rules in its own query language.
"""

from collections.abc import Callable, Iterable

from sdd_core import machine
from sdd_core.models import LIVE_EFFECT_STATUSES, SETTLED_STATUSES, UNPINNED_KINDS, Run, Transition
from sdd_core.records import HANDLER_CHANGED_WHILE_LIVE, Conflict, EffectRecord


def check_dependencies(
    run_id: str, dependencies: Iterable[str], exists: Callable[[str], bool]
) -> tuple[str, ...]:
    """A new run's prerequisites, sorted and unique: each an existing other run."""
    wanted = tuple(sorted(set(dependencies)))
    if run_id in wanted or not all(exists(key) for key in wanted):
        raise ValueError("Invalid dependencies")
    return wanted


def same_input(existing: tuple[object, ...], requested: tuple[object, ...]) -> None:
    """Creating a run again is idempotent only with the same inputs."""
    if existing != requested:
        raise Conflict("Run id reused with different input")


def live_process(kind: str, status: str) -> bool:
    """An effect whose handler process may still run (it locks the pinned manifest)."""
    return kind not in UNPINNED_KINDS and status in LIVE_EFFECT_STATUSES


def pin_changes(old: str | None, manifest: str, live: bool) -> bool:
    """Whether a handler pin must be written: it follows the current manifest between
    attempts, never under a live one."""
    if old == manifest:
        return False
    if old is not None and live:
        raise Conflict(HANDLER_CHANGED_WHILE_LIVE)
    return True


def check_immutable(old: str | None, document: str, message: str) -> None:
    if old is not None and old != document:
        raise Conflict(message)


def check_execution_binding(
    record: EffectRecord, run_id: str, old: tuple[str, str] | None, request: tuple[str, str]
) -> None:
    """An attempt is handed to one execution backend, once, and never after a local host
    took it."""
    if record.run_id != run_id or record.status not in ("pending", "running"):
        raise Conflict("Attempt is not available for execution")
    if old is not None and old != request:
        raise Conflict("Execution request is immutable")
    if record.host_nonce is not None or record.pid is not None:
        raise Conflict("Attempt already belongs to a local host")


def started_unfinished(run: Run) -> bool:
    """A run that holds work in progress: it started and is not accepted."""
    return run.generation > 0 and run.status != "accepted"


def runnable(
    candidates: Iterable[tuple[Run, Iterable[str], float, float]],
) -> tuple[str, ...]:
    """Resumed, unfinished runs whose prerequisites are all accepted, fairest first:
    least recently dispatched (else oldest), then oldest, then by id.

    Each candidate is (run, statuses of its prerequisites, last dispatch or creation
    time, creation time).
    """
    ready = [
        (last, created, run.id)
        for run, prerequisites, last, created in candidates
        if not run.paused
        and run.status not in SETTLED_STATUSES
        and all(status == "accepted" for status in prerequisites)
    ]
    return tuple(identifier for _, _, identifier in sorted(ready))


def check_discard(
    wanted: set[str],
    runs: Callable[[str], Run],
    has_effects: Callable[[str], bool],
    edges: Iterable[tuple[str, str]],
) -> None:
    """Only never-started runs without effects leave, and never one a kept run needs."""
    for identifier in sorted(wanted):
        if not machine.discardable(runs(identifier)):
            raise ValueError(f"Run {identifier} has started; it cannot be discarded")
        if has_effects(identifier):
            raise ValueError(f"Run {identifier} has effects; it cannot be discarded")
    for run_id, prerequisite in edges:
        if prerequisite in wanted and run_id not in wanted:
            raise ValueError(f"Run {run_id} depends on {prerequisite}")


def check_transition(before: Run, transition: Transition) -> None:
    """A transition advances the run's version by one, and changes its workflow only
    as a recorded migration."""
    after = transition.state
    if after.id != before.id or after.version != before.version + 1:
        raise ValueError("Invalid transition version")
    migrated = any(event.kind == "workflow_migrated" for event in transition.events)
    if after.workflow_digest != before.workflow_digest and not migrated:
        raise ValueError("A run changes its workflow only by migration")
