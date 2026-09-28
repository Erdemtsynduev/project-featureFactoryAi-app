# Architecture and contracts

## Dependency direction

`providers -> core <- storage`; `runtime services -> core`; `workflows -> core`;
`usage -> core`; `ui -> core, runtime, providers, workflows, usage`.
`sdd_runtime.composition` is the local composition root (SQLite storage, Git, local files
and the handler registry); the CLI and the UI both compose through it, and the UI never
imports the CLI or `sdd_storage`. Core and usage import no OS, process, filesystem,
database, clock or randomness modules. `tools/check_boundaries.py` enforces all of this.

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

The default Git adapter hashes HEAD plus the content of every path `git status` reports
against it (modified, deleted, renamed and untracked non-ignored files); unchanged tracked
files are identified by HEAD, so large repositories are not re-read per observation. It runs
with `--no-optional-locks` and never rewrites the index. Engine scratch, virtual environments
and generated Python caches are excluded. Symlinked workspaces/files are not silently followed.
Other project revision models can implement ProjectAdapter. Revisions from the earlier
full-content algorithm differ: an unfinished run observes one "Workspace changed" block after
upgrade and continues after an explicit retry.

## Scoped runs in multi-repository workspaces

A run executes in one workspace folder but may own only some sub-folders of it (its scope,
for example `libraries/body_dynamics`). The claim is then a canonical JSON list of absolute
paths; a single path keeps the old plain form. Claims drive admission (overlapping claims
serialize, including against unfinished runs) and the observed revision: one path hashes
that folder, several paths hash the ordered list of per-path revisions. A nested repository
ignored by its parent is therefore still tracked when it is in scope. A scoped path that does
not exist yet has a stable "absent" revision, so a ticket may create its repository. Evidence
still lives under the workspace root. Launch directories may be any folder inside the
workspace (checks with `cwd`), never outside it.

## Conditions

A condition step routes inside the dispatch transaction with no outbox effect or attempt.
`condition_key` is a dotted path into the previous result's `data` object (a human answer
records `{"answer": ...}`); text compares verbatim, other JSON values by canonical spelling,
and a missing key is false. Visit limits still apply. Condition attempts persisted by earlier
releases are released on restore and re-routed purely.

`GitProject.integrate` records a request before fast-forward integration; retries recognize the
already integrated commit. Dirty targets, divergent history and changed bases block. Multi-repo
integration is not a distributed transaction. Callers must rerun acceptance on the integrated target.

Portfolio primitives validate complete requirement coverage and an acyclic ticket dependency graph.
A source requirement closes only when all associated runs are accepted. `PortfolioService.admit`
materializes approved ticket manifests idempotently as paused dependent runs. The CLI also creates
individual runs with dependency IDs; application-specific portfolio import is a later application stage.

## Context and budgets

Original run context is immutable and bounded. Instead of raw receipts, the next packet carries
a task brief (`sdd_core.memory`): the de-duplicated `notes` every earlier step declared, oldest
first, and a compact handoff of the latest three outcomes, fitted into the character budget, plus
a link to the full previous receipt. `assemble` supports revision-scoped records, priorities and
non-truncatable required content. There is no automatic LLM summarizer: the brief is a pure
function of recorded results.

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

## Questions, recommended answers and session continuation

An agent that needs a human decision returns the `questions` outcome together with
structured `questions` (id, question, 2-6 options, one recommended option); the
result schema passed to Claude and Codex requires the field (an empty list otherwise).
A human step may also declare preset `questions` in its config. The UI answers by
choice; the answer text and the chosen options are stored as the human step's result.

`Run.auto_answer` (commands `auto` / `manual`, UI switch "Агент выбирает рекомендованные
ответы") or a human step's `{"auto_answer": "recommended"}` lets the coordinator answer
with every recommendation instead of waiting. It applies only when every question has a
recommendation, only while the queue runs, and records `"auto": true` in the answer.

Answering does not restart the model from scratch. When the only results since the
step's own previous attempt are human answers, the workspace revision is unchanged and
the handler declares the `resume` capability, the next packet carries that native
session id (`claude --resume <id>`, `codex exec resume <id>`) and only the answers
(plus operator guidance). The session id is recorded in the agent's result data before
the continuation starts. Any other step, a changed revision or another handler starts a
fresh session with the full bounded context.

## Transactions, transitions and failure isolation (2026-09-28)

Every change of a run is a named pure transition in `sdd_core.machine` (`dispatch`,
`complete`, `recover`, `control`, `block`, `invalidate`, `relocate`, `reconcile`,
`release_condition`, `guidance`). Runtime code never builds a state with `changed()`;
the boundary gate rejects `machine.changed` outside core.

Slow observations stay outside transactions. `dispatch` and `complete` read a snapshot,
hash the Git revision and evidence without holding the SQLite write lock, then apply the
transition with compare-and-swap on the snapshot version. A concurrent change raises
`StaleVersion` (a `Conflict`) and the use case re-reads and retries a bounded number of
times. An operator answer pins the version it was given, so it is rejected instead of
retried when the task moved. Admission (dependencies, process slots, overlapping claims)
and the queue call budget are decided inside the writing transaction; the budget uses
the backend's `queue_usage()` aggregate. Process slots are `ApplicationEngine`
parameters (two agents, one operation by default).

The persistence transaction is composed of role interfaces in `sdd_core.records`
(`RunRecords`, `CommandLog`, `AdmissionRecords`, `ResultRecords`, `ExecutionRecords`,
`PortfolioRecords`, `LaneRecords`); `UnitOfWork` is their union for backends.

The coordinator only orchestrates: `hosts.py` owns host processes and containment,
`packets.py` builds bounded packets and session continuations, `lane_keeper.py`
opens and removes worktree lanes (recording intent before each repository change).
A failure of one run during a tick is recorded on that run — blocked, or recovered when
it holds an attempt — and the tick continues with the others. Only a failure to record
it stops the queue, because then state could not be kept durable.

Application metadata (projects, plans, task labels, preferences) is behind the
`sdd_core.catalog.CatalogRecords` port. SQLite schema version 3 owns its tables
(adopting the ones earlier releases created from the UI, after a verified backup);
`MemoryCatalog` is the volatile double. The UI has no SQL except the read-only
importer of foreign sdd-orchestrator databases (`sdd_ui.legacy`).

The UI service locks only the coordinator and queue settings. Board reads and engine
commands run concurrently with the queue through short transactions and CAS, so a long
tick never freezes the board. `/api/state` carries an ETag; polling revalidates it and
an unchanged board is answered with 304.

## Operator application layer (2026-09-28)

`sdd_ui.service.WorkspaceService` is only the composition root of the local UI: it
wires use-case services and maps HTTP action names to them.

| Module | Responsibility |
|---|---|
| `queue` | Queue settings, the coordinator's lifecycle, the work loop, status watching and revival after limits |
| `agents` | Profiles file, handler registry replacement, discovery-backed connection, rotation, rest release, Codex quotas |
| `flows` | Templates for a project, validation against installed handlers, publication, readiness per task kind |
| `tasks` | Task creation by kind, commands, messages, answers, ticket admission, recovery with an incident briefing |
| `attention` | The single "why is this task (not) moving" derivation and its resolving action |
| `flightlog`, `diagnostics` | Append-only flight log and per-task incident records |

Every action is recorded in the flight log with its outcome; failures are recorded
before the error reaches the client. The queue watcher logs transitions into
`blocked` and `waiting` once per change.

Decomposition is deterministic. A planning step opts into structured ticket output
with `{"emits": "tickets"}` in its config; the agent result schema then requires
`tickets`, validated by `sdd_core.memory.tickets_of` (unique ids, known acyclic
dependencies) before routing. Only a human approval of the requirement admits them:
the ticket workflow is published first, then the answer is applied, then each ticket
becomes a paused dependent run whose context is the ticket plus the approved
specification. Admission is idempotent per derived run id.

Every agent result may carry `notes` (0-5 durable facts). They form the task memory
shared by later steps and by other agents, so a fallback profile or a fresh session
continues from recorded decisions instead of re-reading transcripts.

Revival: a run blocked by the core's `WAIT_RETRY_LIMIT` (consecutive infrastructure
waits, usually provider limits) is retried by the running queue after 30 minutes of
rest when its agent profile or a rotation member is available, at most five times
per session and only while the `revive` queue setting is on. Queue budgets apply.

The browser client is a set of ES modules (`static/js`) with keyed dictionaries for
Russian and English; it holds no workflow or acceptance logic and renders the
server's attention reason instead of deriving task state itself.
