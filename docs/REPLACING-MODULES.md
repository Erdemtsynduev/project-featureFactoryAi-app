# Replacing engine modules

The application services, coordinator, execution driver, portfolio and diagnostic
archiver use `sdd_core.ports.StateStore` / `UnitOfWork`. They contain no SQL and do
not import a concrete storage package. `RuntimeRecords` describes process claims,
execution requests, bindings and scheduler projections within that same unit of
work. Both are unions of the role interfaces in `sdd_core.records`; a backend must
raise `StaleVersion` when `apply` loses a compare-and-swap and implement
`queue_usage()`. The SQLite implementation owns SQL, table initialization and schema
migrations.

## Composition

```python
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_core.sdk import Registry
from sdd_storage.store import Store
from pathlib import Path

store = Store(Path("state.db"))  # Replace only this construction with your adapter.
engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
registry = Registry()  # Register trusted handlers before starting work.
coordinator = Coordinator(engine, registry, health_path=Path("health.json"))
```

`MemoryStore()` from `sdd_storage.memory` can replace `Store(...)` here without
changing the services. It is an independently implemented, thread-serialized,
transactional backend used for embedding/tests. It has **no persistence** and
must not back an unattended production queue. Runtime contract tests execute real
child processes with both stores; memory-store coordinator reconstruction retains
the same store object and does not simulate machine/process persistence.

The existing `Engine(Store(...))` remains a convenience composition for SQLite,
Git and the local workspace. It is not the base class required by the coordinator.
Health output is an explicit optional path, not inferred from a database filename.
The bundled CLI supplies the previous `.health.json` location.

## Contracts that replacements must preserve

- Commit state, events, effects, receipts and ownership in one atomic transaction.
  Exceptions escaping a unit of work roll back all its changes.
- Serialize ownership decisions. Local host reservation and external execution
  binding are mutually exclusive, including concurrent submissions.
- Reject conflicting command IDs, receipts, manifests and execution requests.
  Identical external requests remain idempotent.
- Bind a host nonce before starting its gated process. Commit PID and process
  creation time against that nonce before sending GO. Unknown identity retains
  ownership; expiry alone never releases it.
- Preserve deterministic scheduler ordering, recent-result ordering, dependency
  and workspace claims. Durable backends must survive a process restart.
- The caller still owns the single-coordinator lease. Storage transactions are
  not a substitute for a distributed lease or remote execution fencing.

`tests/test_runtime_storage_contract.py` is the shared executable contract suite.
Add a backend to its `configured` fixture and run the same scenarios. Also run
`tests/test_application_port.py` and backend-specific crash/persistence tests.
Passing an in-memory contract suite alone does not qualify a durable database.

## Installation boundaries

`sdd-runtime` requires core and psutil, but no storage package. The library services
can be imported without `sdd-storage`; the isolated wheel check verifies this.
`sdd-runtime[local]` adds SQLite composition. The umbrella `feature-factory-ai` still installs
that local composition and the built-in providers/workflows, preserving CLI setup.

Definition publishing, backup, journal inspection and schema migration are
administration capabilities of the chosen backend. The bundled CLI uses SQLite's
administration API; a different backend needs its own administration composition.
That does not require editing coordinator or application use cases.

## Other extension boundaries

| Replace | Contract / composition |
|---|---|
| Agent CLI or model selection | `Handler`, named profiles, `sdd.agents` factories |
| Step implementation | `Handler`, explicit `Registry` / `sdd.handlers` |
| Local or remote execution | `ExecutionBackend`, `ExecutionDriver`, `sdd.executors` |
| Workspace paths and evidence | `Workspace` |
| Revision model | `ProjectAdapter` |
| Workflow | Immutable `Workflow` / `Step` definitions |
| Persistence | `StateStore`, transactional `UnitOfWork` including `RuntimeRecords` |
| Application metadata (UI) | `CatalogRecords` (`Store.catalog()`, `MemoryCatalog`) |

An external library is wrapped in a small adapter satisfying the appropriate
contract. It is not enough to give an arbitrary library matching method names:
transaction, idempotency and termination semantics must also match. OS process
containment remains in the local process implementation; using a different host
means supplying a different execution backend, not putting OS logic in the core.

The boundary gate rejects storage imports outside the local composition
(`sdd_runtime.composition`, `engine.py`) and CLI, rejects raw transaction/SQL calls in
runtime and UI modules, and rejects `machine.changed` outside the core. This keeps replacement
points from gradually becoming coupled again.
