"""Volatile transactional backend for embedding and contract tests; never durable storage."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import RLock
from typing import cast

from sdd_core import machine
from sdd_core.catalog import AgentCall
from sdd_core.codec import digest, result_load, workflow_json
from sdd_core.graph import validate
from sdd_core.models import Attempt, Result, Run, Transition, Workflow
from sdd_core.ports import Conflict, StaleVersion, UnitOfWork
from sdd_core.runtime_ports import EffectRecord


@dataclass
class MemoryState:
    flows: dict[str, Workflow] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    inputs: dict[str, tuple[str, str, str, tuple[str, ...]]] = field(default_factory=dict)
    created: dict[str, float] = field(default_factory=dict)
    events: list[tuple[str, float, str]] = field(default_factory=list)
    effects: dict[str, EffectRecord] = field(default_factory=dict)
    results: dict[str, tuple[str, str]] = field(default_factory=dict)
    commands: dict[str, tuple[str, str]] = field(default_factory=dict)
    bindings: dict[tuple[str, str], str] = field(default_factory=dict)
    executions: dict[str, tuple[str, str]] = field(default_factory=dict)
    portfolios: dict[str, str] = field(default_factory=dict)
    lanes: dict[str, str] = field(default_factory=dict)
    policies: dict[str, tuple[str, ...]] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)


class MemoryUnit:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def runs(self) -> tuple[Run, ...]:
        return tuple(
            sorted(self.state.runs.values(), key=lambda r: (self.state.created[r.id], r.id))
        )

    def run(self, identifier: str) -> Run:
        return self.state.runs[identifier]

    def apply(self, before: Run, transition: Transition) -> Run:
        after = transition.state
        if after.id != before.id or after.version != before.version + 1:
            raise ValueError("Invalid transition version")
        if self.run(before.id).version != before.version:
            raise StaleVersion("Stale state version")
        self.state.runs[before.id] = after
        self.state.events.extend((before.id, event.at, event.kind) for event in transition.events)
        for effect in transition.effects:
            if effect.id in self.state.effects:
                raise Conflict("Duplicate effect")
            self.state.effects[effect.id] = EffectRecord(
                effect.id, before.id, effect.kind, "pending", self.location(before.id)[0]
            )
            self.state.attempts[effect.id] = effect.attempt
        return after

    def command(self, identifier: str) -> tuple[str, str] | None:
        return self.state.commands.get(identifier)

    def save_command(self, identifier: str, request: str, response: str) -> None:
        if identifier in self.state.commands:
            raise Conflict("Duplicate command")
        self.state.commands[identifier] = (request, response)

    def location(self, identifier: str) -> tuple[str, str]:
        workspace, _, claim, _ = self.state.inputs[identifier]
        return workspace, claim

    def locations(self) -> tuple[tuple[str, str], ...]:
        return tuple((key, inputs[0]) for key, inputs in self.state.inputs.items())

    def dependency_edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (key, prerequisite)
            for key, inputs in sorted(self.state.inputs.items())
            for prerequisite in inputs[3]
        )

    def policy(self, workspace: str) -> tuple[str, ...]:
        return self.state.policies.get(workspace, ())

    def set_policy(self, workspace: str, mandatory: tuple[str, ...]) -> None:
        self.state.policies[workspace] = tuple(sorted(set(mandatory)))

    def dependencies(self, identifier: str) -> tuple[Run, ...]:
        return tuple(self.run(key) for key in self.state.inputs[identifier][3])

    def active_claims(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (record.kind, self.location(record.run_id)[1])
            for record in self.effects(("pending", "running", "uncertain"))
            if record.kind not in ("human", "condition")
        )

    def unfinished_claims(self, identifier: str) -> tuple[tuple[Run, str], ...]:
        return tuple(
            (run, self.location(key)[1])
            for key, run in self.state.runs.items()
            if key != identifier and run.generation > 0 and run.status != "accepted"
        )

    def results(self, identifier: str) -> tuple[Result, ...]:
        return tuple(
            result_load(document)
            for key, (_, document) in self.state.results.items()
            if self.effect(key).run_id == identifier
        )

    def receipt(self, attempt: str) -> tuple[str, str] | None:
        entry = self.state.results.get(attempt)
        return None if entry is None else (entry[0], self.effect(attempt).run_id)

    def save_result(self, attempt: str, fingerprint: str, document: str) -> None:
        if attempt in self.state.results:
            raise Conflict("Duplicate result")
        self.state.results[attempt] = (fingerprint, document)
        self.state.effects[attempt] = replace(self.effect(attempt), status="done", receipt=document)

    def effect_status(self, attempt: str, status: str) -> None:
        self.state.effects[attempt] = replace(self.effect(attempt), status=status)

    def bind_handler(self, run_id: str, handler: str, manifest: str) -> None:
        key = (run_id, handler)
        old = self.state.bindings.get(key)
        if old is not None and old != manifest:
            raise Conflict("Pinned handler settings or version changed")
        self.state.bindings[key] = manifest

    def context(self, run_id: str) -> str:
        return self.state.inputs[run_id][1]

    def set_context(self, identifier: str, context: str) -> None:
        workspace, _, claim, dependencies = self.state.inputs[identifier]
        self.state.inputs[identifier] = workspace, context, claim, dependencies

    def recent_results(self, run_id: str, limit: int) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("Positive result limit required")
        return tuple(
            self.state.results[key][1]
            for key in reversed(self.state.effects)
            if key in self.state.results and self.effect(key).run_id == run_id
        )[:limit]

    def step_results(self, run_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (self.state.attempts[key].step, self.state.results[key][1])
            for key in reversed(self.state.effects)
            if key in self.state.results and self.effect(key).run_id == run_id
        )

    def effect(self, attempt: str) -> EffectRecord:
        return self.state.effects[attempt]

    def effects(self, statuses: tuple[str, ...]) -> tuple[EffectRecord, ...]:
        return tuple(record for record in self.state.effects.values() if record.status in statuses)

    def claim_host(self, attempt: str, packet: str, nonce: str) -> None:
        record = self.effect(attempt)
        if (
            record.external
            or record.status != "pending"
            or record.host_nonce is not None
            or record.pid is not None
        ):
            raise Conflict("Attempt already claimed")
        self.state.effects[attempt] = replace(record, host_nonce=nonce)

    def host_started(self, attempt: str, nonce: str, pid: int, created: float) -> None:
        record = self.effect(attempt)
        if record.status != "pending" or record.host_nonce != nonce or record.pid is not None:
            raise Conflict("Dispatch ownership changed before launch")
        self.state.effects[attempt] = replace(record, status="running", pid=pid, created=created)

    def execution(self, attempt: str) -> tuple[str, str] | None:
        return self.state.executions.get(attempt)

    def bind_execution(self, run_id: str, attempt: str, backend: str, document: str) -> None:
        record = self.effect(attempt)
        if record.run_id != run_id or record.status not in ("pending", "running"):
            raise Conflict("Attempt is not available for execution")
        old = self.execution(attempt)
        if old is not None and old != (backend, document):
            raise Conflict("Execution request is immutable")
        if record.host_nonce is not None or record.pid is not None:
            raise Conflict("Attempt already belongs to a local host")
        self.state.executions[attempt] = (backend, document)
        self.state.effects[attempt] = replace(record, external=True)

    def runnable(self) -> tuple[str, ...]:
        def priority(identifier: str) -> tuple[float, float, str]:
            dispatched = [
                at
                for key, at, kind in self.state.events
                if key == identifier and kind == "dispatched"
            ]
            created = self.state.created[identifier]
            return max(dispatched, default=created), created, identifier

        return tuple(
            sorted(
                (
                    key
                    for key, run in self.state.runs.items()
                    if not run.paused
                    and run.status not in ("accepted", "blocked")
                    and all(
                        self.state.runs[needed].status == "accepted"
                        for needed in self.state.inputs[key][3]
                    )
                ),
                key=priority,
            )
        )

    def queue_usage(self) -> tuple[int, int]:
        runs = self.state.runs.values()
        return sum(r.calls for r in runs), sum(r.planning_calls for r in runs)

    def last_transition(self) -> float | None:
        return max((at for _, at, _ in self.state.events), default=None)

    def lane(self, run_id: str) -> str | None:
        return self.state.lanes.get(run_id)

    def save_lane(self, run_id: str, document: str) -> None:
        self.run(run_id)
        self.state.lanes[run_id] = document

    def relocate(self, identifier: str, workspace: str, claim: str) -> None:
        _, context, _, dependencies = self.state.inputs[identifier]
        self.state.inputs[identifier] = workspace, context, claim, dependencies

    def portfolio(self, identifier: str) -> str | None:
        return self.state.portfolios.get(identifier)

    def bind_portfolio(self, identifier: str, document: str) -> None:
        old = self.portfolio(identifier)
        if old is not None and old != document:
            raise Conflict("Approved portfolio revision is immutable")
        self.state.portfolios[identifier] = document


class MemoryStore:
    """Thread-serialized transactions. Data disappears when this object is discarded."""

    def __init__(self) -> None:
        self._state = MemoryState()
        self._lock = RLock()
        self._in_transaction = False

    @contextmanager
    def unit(self) -> Iterator[UnitOfWork]:
        with self._lock:
            if self._in_transaction:
                raise RuntimeError("Nested units of work are not supported")
            self._in_transaction = True
            try:
                candidate = deepcopy(self._state)
                yield MemoryUnit(candidate)
                self._state = candidate
            finally:
                self._in_transaction = False

    def publish(self, workflow: Workflow, mandatory: tuple[str, ...] = ()) -> str:
        validate(workflow, mandatory)
        key = digest(workflow_json(workflow))
        with self._lock:
            self._state.flows[key] = workflow
        return key

    def workflow(self, identifier: str) -> Workflow:
        with self._lock:
            return self._state.flows[identifier]

    def get(self, identifier: str) -> Run:
        with self._lock:
            return self._state.runs[identifier]

    def discard(self, identifiers: tuple[str, ...]) -> tuple[str, ...]:
        wanted = set(identifiers)
        with self._lock:
            state = self._state
            for identifier in sorted(wanted):
                if not machine.discardable(state.runs[identifier]):
                    raise ValueError(f"Run {identifier} has started; it cannot be discarded")
                if any(effect.run_id == identifier for effect in state.effects.values()):
                    raise ValueError(f"Run {identifier} has effects; it cannot be discarded")
            for key, inputs in state.inputs.items():
                if key not in wanted and wanted & set(inputs[3]):
                    raise ValueError(f"Run {key} depends on a discarded run")
            for identifier in wanted:
                for table in (state.runs, state.inputs, state.created, state.lanes):
                    table.pop(identifier, None)
            state.events[:] = [event for event in state.events if event[0] not in wanted]
            for binding in [b for b in state.bindings if b[0] in wanted]:
                del state.bindings[binding]
        return tuple(sorted(wanted))

    def create(
        self,
        run: Run,
        workspace: str,
        context: str,
        claim: str,
        now: float,
        dependencies: tuple[str, ...] = (),
    ) -> Run:
        inputs = (workspace, context, claim, tuple(sorted(set(dependencies))))
        with self._lock:
            old = self._state.runs.get(run.id)
            if old:
                if (
                    old.workflow_digest != run.workflow_digest
                    or self._state.inputs[run.id] != inputs
                ):
                    raise Conflict("Run id reused with different input")
                return old
            if run.workflow_digest not in self._state.flows:
                raise KeyError(run.workflow_digest)
            if run.id in dependencies or any(key not in self._state.runs for key in dependencies):
                raise ValueError("Invalid dependencies")
            self._state.runs[run.id] = run
            self._state.inputs[run.id] = inputs
            self._state.created[run.id] = now
            self._state.events.append((run.id, now, "created"))
            return run


class MemoryCatalog:
    """Volatile CatalogRecords over a MemoryStore, for embedding and contract tests."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.documents: dict[str, dict[str, str]] = {
            "projects": {},
            "tasks": {},
            "preferences": {},
        }
        self.plan_documents: dict[tuple[str, str], str] = {}
        self.artifact_documents: dict[tuple[str, str], str] = {}

    def projects(self) -> tuple[str, ...]:
        return tuple(v for _, v in sorted(self.documents["projects"].items()))

    def save_project(self, identifier: str, document: str) -> None:
        self.documents["projects"][identifier] = document

    def plans(self) -> tuple[tuple[str, str], ...]:
        return tuple((key[0], value) for key, value in sorted(self.plan_documents.items()))

    def save_plans(self, project: str, plans: tuple[tuple[str, str], ...]) -> None:
        for identifier, document in plans:
            self.plan_documents[project, identifier] = document

    def tasks(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.documents["tasks"].items())

    def save_task(self, identifier: str, document: str) -> None:
        self.store.get(identifier)
        self.documents["tasks"].setdefault(identifier, document)

    def update_task(self, identifier: str, document: str) -> None:
        if identifier not in self.documents["tasks"]:
            raise KeyError(identifier)
        self.documents["tasks"][identifier] = document

    def artifacts(self, run: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (kind, value)
            for (key, kind), value in sorted(self.artifact_documents.items())
            if key == run
        )

    def save_artifact(self, run: str, kind: str, document: str) -> None:
        self.store.get(run)
        self.artifact_documents[run, kind] = document

    def preference(self, key: str) -> str | None:
        return self.documents["preferences"].get(key)

    def save_preference(self, key: str, document: str) -> None:
        self.documents["preferences"][key] = document

    def agent_calls(self) -> tuple[AgentCall, ...]:
        with self.store.unit() as unit:
            state = cast(MemoryUnit, unit).state
            return tuple(
                AgentCall(
                    record.run_id,
                    state.runs[record.run_id].workflow_digest,
                    state.attempts[key].step,
                    state.attempts[key].started,
                    record.status,
                    state.results[key][1] if key in state.results else None,
                )
                for key, record in state.effects.items()
                if record.kind == "agent"
            )

    def daily_dispatches(self, days: int) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        with self.store.unit() as unit:
            for _, at, kind in cast(MemoryUnit, unit).state.events:
                if kind == "dispatched":
                    day = datetime.fromtimestamp(at, UTC).date().isoformat()
                    counts[day] = counts.get(day, 0) + 1
        return tuple(sorted(counts.items(), reverse=True)[:days])
