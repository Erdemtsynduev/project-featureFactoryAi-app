"""What every application service shares: the ports, the workflow cache and the
optimistic write loop.

Slow observations (Git revisions, evidence hashes) run outside transactions; the
write then compares-and-swaps the run version and retries if it moved meanwhile.
"""

from collections.abc import Callable

from sdd_core.codec import canonical, digest, run_load
from sdd_core.models import Result, Run, Workflow
from sdd_core.ports import Conflict, StaleVersion, StateStore, Workspace
from sdd_core.records import CommandLog, ResultRecords
from sdd_core.sdk import ProjectAdapter

RETRIES = 3


class RunContext:
    """The ports of one engine plus the facts derived from them."""

    def __init__(self, store: StateStore, project: ProjectAdapter, workspace: Workspace) -> None:
        self.store = store
        self.project = project
        self.workspace = workspace
        # Published workflows are immutable and content-addressed: safe to cache.
        self._flows: dict[str, Workflow] = {}

    def flow(self, identifier: str) -> Workflow:
        if identifier not in self._flows:
            self._flows[identifier] = self.store.workflow(identifier)
        return self._flows[identifier]

    def cached_flow(self, identifier: str) -> Workflow | None:
        """A workflow already read; transactions never read one themselves."""
        return self._flows.get(identifier)

    def workflow_of(self, run_id: str) -> Workflow:
        return self.flow(self.store.get(run_id).workflow_digest)

    def revision_of(self, claim: str) -> str:
        paths = self.workspace.paths(claim)
        if len(paths) == 1:
            return self.project.revision(paths[0])
        return digest(canonical([[path, self.project.revision(path)] for path in paths]))

    def observe(self, run_id: str) -> str:
        """Current revision of everything the run owns."""
        with self.store.unit() as db:
            claim = db.location(run_id)[1]
        return self.revision_of(claim)


def optimistic[T](attempt: Callable[[], T], exhausted: str) -> T:
    """Run a read-observe-write attempt until its compare-and-swap holds."""
    for _ in range(RETRIES):
        try:
            return attempt()
        except StaleVersion:
            continue
    raise Conflict(exhausted)


def replayed(db: CommandLog, request_id: str, request: str) -> Run | None:
    """The response of an already applied command; its id reused for other input fails."""
    old = db.command(request_id)
    if old is None:
        return None
    if old[0] != request:
        raise Conflict("Command id reused")
    return run_load(old[1])


def received(db: ResultRecords, attempt: str, receipt: tuple[str, str]) -> bool:
    """Whether this very result was applied; another result for the attempt fails."""
    old = db.receipt(attempt)
    if old and old != receipt:
        raise Conflict("Conflicting repeated result")
    return bool(old)


def data_of(results: tuple[Result, ...], attempt: str | None) -> str:
    """The data object the given attempt returned; empty when there is none."""
    if attempt is None:
        return "{}"
    return next((r.data for r in results if r.attempt_id == attempt), "{}")
