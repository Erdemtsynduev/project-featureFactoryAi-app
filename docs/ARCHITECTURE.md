# Architecture and contracts

## Dependency direction

`providers -> core <- storage`; `runtime services -> core`; `local composition -> storage`; `workflows -> core`.
The CLI is the explicit application composition root and imports optional providers/templates.
Core imports no OS, process, filesystem, database, clock or randomness modules.

Immutable dataclasses define Workflow, Step, Run, Attempt, Result, Artifact, Usage,
Transition, Event and Effect. The graph wire schema is exported by `sdd schema`.
SDK API version 1 is checked before a plugin registers. The registry rejects duplicates.
Breaking schemas/APIs require a new version and migration; published definitions are immutable.

## One writer, durable effects

`Store.apply` atomically writes a CAS-protected run projection, journal entries and outbox effects.
`Engine.command` records request IDs and their original response; reusing an ID with other
input is an error. Results are keyed by attempt ID and digest; conflicting receipts are rejected.
The journal contains the resulting typed snapshot for each transition. `replay` validates
contiguous versions and reconstructs the projection without executing any external effect.
This is snapshot-assisted journal replay, not arbitrary historical engine-version emulation.

`dispatch` is a pure function with injected time and ID. Scheduling transactionally reserves
the workspace claim. An expired heartbeat/deadline never frees an unknown process owner.
Dependencies must be accepted; overlapping workspace paths serialize. Independent workspaces
allow two agent activities and one non-agent operation. Human waiting does not occupy a process slot.

At launch the engine persists an outbox row, materializes immutable attempt files and starts a
small host waiting on stdin. It assigns aggregate and per-attempt Windows Jobs, persists PID,
creation time and nonce, then sends GO. Before GO, parent death yields EOF and no payload runs.
The aggregate job caps CPU at 50%, committed memory at 16 GiB and processes at 128.

The host records exit code, nonce and completion time. On collection, the coordinator confirms
the owned job is empty, hashes evidence, writes a receipt, then applies the result transactionally.
Recovery can collect an exit receipt or reapply a saved result without re-executing the task.
No claim of exactly-once external side effects is made. Missing evidence requires reconciliation.
An uncertain live PID is retained and blocked, never killed by name or opportunistically retried.

User pause is independent from process status and survives recovery. Infrastructure retries have
their own counter (three automatic retries with exponential delays); product visits stay bounded.
Supervisor restart allowance is persisted and requires an explicit reset after exhaustion.

## Acceptance and revision

Required execution and revision-bound gates are separate flags. Gate steps are read-only and
must return passed artifacts for the current revision. Agent gates require both Standards and
Spec. Mutating dispatch invalidates old gates before execution. Finish rechecks file digests and
the current workspace revision. The same checks apply through the Engine API, not only the CLI.
For a non-gate, `required` means a declared result was processed, including an explicit failed
outcome that routes to repair. It does not turn a failure into a passing quality gate. Use `gate`
when a successful verification, rather than execution, is required.

The default Git adapter hashes HEAD and tracked/untracked non-ignored files. Engine scratch,
virtual environments and generated Python caches are excluded. Symlinked workspaces/files are
not silently followed. Other project revision models can implement ProjectAdapter.

`GitProject.integrate` records a request before fast-forward integration; retries recognize the
already integrated commit. Dirty targets, divergent history and changed bases block. Multi-repo
integration is not a distributed transaction. Callers must rerun acceptance on the integrated target.

Portfolio primitives validate complete requirement coverage and an acyclic ticket dependency graph.
A source requirement closes only when all associated runs are accepted. `PortfolioService.admit`
materializes approved ticket manifests idempotently as paused dependent runs. The CLI also creates
individual runs with dependency IDs; application-specific portfolio import is a later application stage.

## Context and budgets

Original run context is immutable and bounded. Recent receipts are included within the character
budget; otherwise a local receipt link is supplied. `assemble` supports revision-scoped records,
priorities and non-truncatable required content. There is no automatic LLM summarizer.

Model calls are conservatively reserved on dispatch. An infrastructure launch failure can consume
a call reservation even if no provider request was sent; it cannot cause extra unbudgeted calls.
Measured input/output/cache usage is retained in results; unknown values stay unknown. Configured
token budgets deny the next dispatch after exhaustion/unknown measurement. They cannot guarantee
the exact maximum tokens used inside a provider CLI attempt. Waiting performs no model calls.

Provider adapters currently block on unclassified CLI failures. A trusted adapter may return
`waiting` with an explicit bounded reset timestamp; guessed reset times are not manufactured.

## Storage and operations

SQLite uses FULL synchronous WAL and BEGIN IMMEDIATE. Schema 1 -> 2 creates a verified backup
before migration. Unsupported future versions are rejected. Backup uses SQLite's online API.
Diagnostics archival only handles settled logs older than retention that are not evidence references;
unfinished work and acceptance evidence are retained.

The local runtime has Windows process-test evidence. Automatic desktop start and
self-modification are not part of this release. The optional sdd-ui application
provides loopback HTTP and a visual editor without reversing library dependencies.

## Application ports and named profiles (2026-09-27)

`ApplicationEngine` depends on core `StateStore`, transactional `UnitOfWork`,
`Workspace` and `ProjectAdapter` contracts. It contains no SQL, subprocess calls or
concrete persistence imports. `Engine` is the local composition root supplying
SQLite, Git and local files. Application contract tests run against SQLite and an
independent transactional memory test double, including rollback/idempotency.
Coordinator, execution driver, portfolio and maintenance now use the same storage
ports. Runtime has no SQL and no required storage-package dependency. SQLite and
an independent volatile MemoryStore implement the transaction contract. The local
CLI remains SQLite administration composition. See
[REPLACING-MODULES.md](REPLACING-MODULES.md) for replacement and verification.

Profile resolution is pure; machine installations/discovery and native CLI auth
are outside core. A profile snapshot is part of the workflow digest and bindings
reject configuration drift. Agent adapter entry points compose factories without
vendor branches in scheduling. See [AGENT-PROFILES.md](AGENT-PROFILES.md).

Ruff checks all Python; strict mypy checks all library sources, operational tools
and the external extension example. The architecture gate also forbids concrete
storage/provider/subprocess imports in application use cases. Dependency inversion
is tested at actual effect boundaries; there is no abstract base class for every
helper function.
