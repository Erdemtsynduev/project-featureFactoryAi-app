# Architecture and contracts

## Dependency direction

`providers -> core <- storage`; `runtime services -> core`; `workflows -> core`;
`usage -> core`; `trackers -> core`; `factory -> core, runtime, workflows`;
`ui -> factory, core, runtime, providers, workflows, usage`.
`sdd_runtime.composition` is the local composition root (SQLite storage, Git, local files
and the handler registry); the CLI and the UI both compose through it, and the UI never
imports the CLI or `sdd_storage`. Core and usage import no OS, process, filesystem,
database, clock or randomness modules. `tools/check_boundaries.py` enforces all of this.

Immutable dataclasses define Workflow, Step, Run, Attempt, Result, Artifact, Usage,
Transition, Event and Effect. The graph wire schema is exported by `sdd schema`.
SDK API version 1 is checked before a plugin registers. The registry rejects duplicates.
Breaking schemas/APIs require a new version and migration; published definitions are immutable.

## The state machine

`sdd_core.machine` holds every rule that changes a run as a pure function
`(run, input, now, ids) -> Transition(state, events, effects)`. Each rule decides
the next state and passes it to one builder, which checks it before the version
advances:

- the status change must be in `models.STATUS_CHANGES` (`accepted` is final);
- `running` has a live attempt; `ready`, `waiting` and `accepted` have none;
- a `blocked` run always has a typed `cause` and a readable `reason`, and only a
  blocked run carries a rule's cause (`stop` may mark any unfinished run).

Behaviour reads `Run.cause` (`call_limit`, `wait_limit`, `uncertain`, `stop`, …),
never the reason text. Model use lives in `Run.spend`; on the wire it stays flat
(`calls`, `tokens`, …) because storage queries read those keys. Every document
leaves and enters through `codec.encode` / `codec.decode`, which read field types,
defaults and vocabularies from the model declarations.

`ApplicationEngine` is a facade over `RunCommands`, `Scheduler`, `ResultIntake` and
`HumanAnswers`, which share a `RunContext` (ports, workflow cache, revisions) and one
optimistic compare-and-swap loop. Pure admission rules are `sdd_core.admission`.
`tests/test_simulation.py` drives seeded schedules of commands, results, faults and
restarts through these services and checks the invariants after every step.

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

Every attempt takes one path. The engine persists an outbox row; the step's handler prepares a
launch; `ExecutionDriver.submit` stores the immutable request (a launch `Plan`) before the local
`Supervisor` starts it; each tick `ExecutionDriver.poll` applies the supervisor's observation.
Other `ExecutionBackend`s (HTTP, installed executors) use the same driver.

The supervisor starts a small host waiting on stdin inside a `Sandbox`: the host starts suspended
and runs only once the aggregate and per-attempt Windows Jobs hold it; its PID, creation time
and nonce are written to `identity.json`, then GO is sent. Before GO, parent death yields EOF
and no payload runs. The aggregate job caps CPU at 50%, committed memory at 16 GiB and
processes at 128.

A job is not complete containment: a packaged launcher (the Python install manager's
`python.exe` alias) lets its interpreter's children break away into no job at all. The sandbox
therefore also keeps a durable lineage of every descendant by parent links, observed each tick,
and records escapes in `containment.json`. Ending an attempt ends the job and then every
remembered descendant; one that will not end leaves the attempt `unknown`, never `terminated`.
The host's PATH puts the interpreters behind such aliases ahead of them, so escapes stay rare.

The host records exit code, nonce and completion time. The supervisor reports an end only once
the sandbox is confirmed empty; the coordinator then hashes evidence, writes a receipt and
applies the result transactionally. A timeout or an operator stop cancels through the driver,
which names the reason from the run itself.
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

A revision names content (`sdd_core.revision`, format `c1:<digest>`). The Git adapter names
every file by the id of its current content: the index blob when the working file matches it,
else Git's own hash of the working file (`hash-object`, with the repository's filters), or
deleted. Committing or staging unchanged content therefore keeps the revision; any edit moves
it. Unchanged files come from the index, so large repositories are not re-read. It runs with
`--no-optional-locks` and never rewrites the index. Engine scratch, virtual environments and
Python caches are excluded; links are not silently followed. A run recorded with an earlier
revision format is re-based once (`revision_rebased`, gates proved again), not blocked.

## Changing the flow of work in progress

A published workflow never changes. A flow change makes a new version: skip a step (routes
into it go where it continued; a required step only on a person's decision), insert one, run
a step with another agent profile, or set a step option (`sdd_core.editor`). `machine.migrate`
moves an idle, unfinished run onto a version: it stays at its step (or where a skipped step
continued), visits and completed steps stay with the steps that remain, and a gate stays
passed only where its step does the same work. Storage accepts a new workflow digest only as a
recorded `workflow_migrated` transition, and the project's mandatory gates still hold.
A person changes one task's flow from its drawer or moves unfinished work onto the current
versions of its templates (`flows-update`); the lead may propose `reflow` for an idle
ticket, and a revised ticket moves to the flow of the repository it now changes.

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

Model calls are reserved on dispatch, and the attempt records its reservation. The reservation
returns when the attempt provably reached no model: its host ended before the payload started
(no host identity or payload log, a failed preflight), or the provider refused at once (a
`REFUSALS` failure with no measured tokens). Such an attempt still counts against the
infrastructure retries and leaves token accounting known. Measured input/output/cache usage is retained in results; unknown values stay unknown. Configured
token budgets deny the next dispatch after exhaustion/unknown measurement. They cannot guarantee
the exact maximum tokens used inside a provider CLI attempt. Waiting performs no model calls.

Provider adapters currently block on unclassified CLI failures. A trusted adapter may return
`waiting` with an explicit bounded reset timestamp; guessed reset times are not manufactured.
Reset moments are read from epochs, "in N hours" and absolute local times ("try again at
Oct 4th, 2026 11:41 PM", "resets 3pm"), bounded to a week.

## Handlers are pinned per attempt
An attempt runs with the handler manifest of its step at dispatch, and `handler.json` in its
folder records it. Between attempts a run's binding follows the installed handler, so an agent
CLI update, an engine upgrade or a profile edit applies from the next step. While an attempt of
the run is live, a changed handler is a `Conflict`: the coordinator waits and never blocks.

## One repository per ticket; the engine commits and pins
A ticket changes exactly one repository. Work across repositories is a chain of tickets: the
dependency's ticket first, then the ticket of the repository that uses it. The second ticket
`depends_on` the first, advances its pin and verifies the integration. The planner is told
this, and approving a breakdown with a ticket spanning repositories is refused with the list.

Repositories of a workspace may pin each other. `sdd_core.links` holds the rules as pure
functions (which pins are stale, which dependency is missing, where a new link goes, following
its neighbours). The `RepositoryLinks` port reads and writes pins. The first adapter is
`GitSubmodules` in `sdd_runtime.submodules`, the only code that knows `.gitmodules`; the
adapter is chosen per repository by what it contains.

**A project receives only its code.** Everything the engine writes for a run lives in its
work folder, `<database name>.work` beside the database (`FFAI_WORK` overrides it):
`engine/<run>/<attempt>/` holds the packet, logs and receipt of each attempt and
`engine/<run>/lane.json` the lane's record; `lanes/<run>/` is the lane itself, a git
worktree per owned repository with the rest of the workspace linked in. The `Workspace`
port names both places (`folder`, `lane`). In a project the engine leaves the branch
`ffai/<run>`, the worktree's registration inside `.git` while the lane exists, and the
merged commits: no file of the working tree and no ignore rule.

A lane checks out every linked dependency at its pinned commit where the repository links it
(a detached worktree of the main dependency), so the lane builds and runs like the workspace.
Isolated ticket flows run a deterministic `commit` operation (`lane-commit`) after every agent
pass:
1. commit the agent's work with the project's template (`commit_message`, default
   `feat({repo}): {title}`);
2. advance the pin of every dependency the run's accepted prerequisites delivered
   (`Packet.delivered`) to its main head, one commit each (`pin_message`, default
   `build({repo}): pin {dependency} {sha}`);
3. link a delivered dependency the repository does not link yet the way its neighbours are
   linked, or block with the reason when there is no pattern.

Commits use the repository's configured author. Checks and review therefore always see a
committed, consistently pinned repository. Agents in lanes are told not to commit or edit pin
files.

## Plan reviews: the lead proposes, a person decides

An approved breakdown is not frozen. A plan review runs when a ticket's own agent
returns `blocked` or a ticket exhausts a step's visits. None runs right after a breakdown
is approved: a person has just decided on those tickets. At most one runs per feature
at a time, one runs per trigger, and a feature gets at most `MAX_REVIEWS`. The **lead**
is one of a project's four roles (`Roles`: lead, analyst, implementer, reviewer); the
project's `roles` setting names the agent profile of each, and the templates take them
as their step handlers. The review is a
workflow (`plan-review`): its read-only agent step `produces: plan_changes` from a brief
that lists every ticket with its state, blocking reason, needs, dependencies and owned
paths, plus what the project's ticket agents can do. The capabilities text is derived
from the ticket flow itself (`templates.capabilities`). A person then decides on the
proposal in "Needs you". The agent never changes the plan on its own decision.

`sdd_core.plan_changes` holds every rule:
- Six change kinds: `revise`, `merge`, `split`, `cancel`, `need` and `guide`.
- Structural changes touch only never-started tickets.
- Needs and guidance may address any unfinished ticket.
- The revised plan must stay a valid, acyclic breakdown.
- Dependents of a removed ticket are rewired: to the merge target, to all split parts,
  or to the cancelled ticket's own prerequisites.

`sdd_factory.reviews.PlanReviews` applies an approved `Revision` in this order:
1. create split parts;
2. `Engine.revise` (brief, owned paths and prerequisites of a never-started run, in one
   transaction, cycle-checked);
3. discard removed tickets;
4. guidance, then an optional retry;
5. hold tickets that need a person or an asset.

Every command is keyed by review, run and verb, so a repeated apply changes nothing.
The review's `plan_changes` artifact is written last; `reapply` finishes an interrupted
apply on start.

Tickets carry typed `needs` (`human`, `asset`, `web`). Held needs (a person, an asset)
keep a ticket out of bulk starts. Breakdowns written before this read a `HITL` goal prefix
as `human`. A person deciding (a human step) holds no workspace claim; only a live
process or a started mutation does.

Agents get the internet only where a step grants it (`tools: ["web"]`):
- Claude's print mode then allows `WebSearch,WebFetch`.
- Codex runs with `web_search="live"`.
- A project enables it for its ticket flow with the `web` setting.
- Publishing refuses a tool the step's handler does not declare.

## Subscriptions are paced by their windows

Subscriptions (Claude Pro/Max, a ChatGPT plan for Codex) are not billed per call; they refuse
work once a five-hour or weekly window is spent. `sdd_usage.quota` models the windows and
decides, purely, when a subscription can take no work (`Quota.rest_until`), which rest a
report asks for (`next_rest`) and when to read again (`next_reading`: every 15 minutes, every 5
near a limit, at once after a refusal, backing off on errors). `sdd_ui.subscriptions` reads
Claude's account usage endpoint with the CLI's own sign-in and Codex's app-server, neither
starting a model turn, and writes the decided rests to the profiles' cooldowns. The
coordinator does not start an agent step whose profile (and every rotation member) rests,
so a spent subscription costs no attempt and work resumes by itself at the reset. A rest a
refusal set is never lifted by a report, which may omit the limit that refused.

The queue-wide call cap (`sdd_core.admission.QueueBudget`) is optional and meant for agents
billed per token; by default there is none. Workflow `max_calls` stays a per-run guard
against runaway loops and counts only calls that may have reached a model.

## Storage and operations

Stored data moves forward once. Schema changes are idempotent SQL applied on every open;
a rewrite of stored data is keyed by the schema version that introduced it
(`sdd_storage.migrations.DATA_MIGRATIONS`) and runs once, after the store backs the
database up. Version 6 records every run's cause explicitly and every feature's kind, so
no reader infers them any more. Records that are immutable by design (published
workflows, result receipts) are read as written: a ticket draft without `needs` still
reads a leading `HITL` goal as a person's decision. Lanes and revisions of an earlier
format move forward lazily, when their run next starts.

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
`migrate`, `rebase_revision`, `guidance`, `plan_revised`). Runtime code never builds a state with `changed()`;
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

The coordinator only orchestrates: `supervisor.py` and `sandbox.py` own processes and containment,
`packets.py` builds bounded packets and session continuations, `lane_keeper.py`
opens and removes worktree lanes (recording intent before each repository change).
A failure of one run during a tick is recorded on that run — blocked, or recovered when
it holds an attempt — and the tick continues with the others. Only a failure to record
it stops the queue, because then state could not be kept durable.

Application metadata (projects, plans, task labels, preferences) is behind the
`sdd_core.catalog.CatalogRecords` port. SQLite schema version 4 owns its tables
(adopting the ones earlier releases created from the UI, after a verified backup);
`MemoryCatalog` is the volatile double. Neither the factory nor the UI runs SQL.

The UI service locks only the coordinator and queue settings. Board reads and engine
commands run concurrently with the queue through short transactions and CAS, so a long
tick never freezes the board. `/api/state` carries an ETag; polling revalidates it and
an unchanged board is answered with 304.

## The factory: application layer (sdd-factory)

`sdd-factory` is the product between the engine and its interfaces. It depends on
core, runtime and workflows; the UI (`sdd-ui`) is an HTTP adapter and console over it.

| Module | Responsibility |
|---|---|
| `model` | The vocabulary, typed: `TaskRecord` (draft, feature, ticket, task), `Children` (what a parent's breakdown becomes), intents, the language rule |
| `catalog` | `ProjectCatalog`: projects (with checks per repository), task records (correctable), artifacts, a parent's stored breakdown |
| `work` | `WorkCreation`: creating work by intent, renaming, the question demo |
| `control` | `WorkControl`: operator commands, bulk start/pause with prerequisites, messages, recovery |
| `admission` | `BreakdownAdmission`: approving a breakdown (a draft's features cover every row once; a feature's tickets change one repository each, `after`), admitting each child with its flow and brief, finishing an interrupted approval |
| `briefs` | What a draft's lead and a feature's analyst are given: pure text builders |
| `documents` | `FeatureDocuments`: a parent's specification, the breakdown awaiting approval, exported documents |
| `supersession` | `Supersession`: closing a parent early, handing work to the feature that plans it again |
| `reviews` | `PlanReviews`: the plan lead's reviews, from trigger to applied revision |
| `reflow` | `FlowChanges`: changing a task's flow, moving outdated work to current templates |
| `drafts` | `DraftIntake`: a source item becomes one draft, once; follow-up drafts for new rows; taking in again what never started |
| `sources` | `MarkdownPlans` (numbered `NNN_*.md` in the folder a project names); `trackers` lists every source a project names |
| `flows` | Templates for a project (draft, feature, ticket with its repositories' checks, main flow), validation, publication |
| `journal`, `diagnostics` | Append-only flight log and per-task incident records |

Work is broken down twice by the same mechanism. A **draft** (an idea, a Markdown
plan, a tracker item; imported once) has a `groom` step (`produces: features`): the
lead cuts it into features, each covering some of the draft's rows. A **feature** has a
specification step (`produces: specification`) and a breakdown step (`produces:
tickets`). Both breakdowns are the same drafts (`sdd_core.tickets.TicketDraft`,
validated by `drafts_of`), approved by the operator and admitted by
`BreakdownAdmission`; `model.Children` says what a parent's children are (their kind,
which names their flow template, and the result key) and the admission holds one brief
builder per kind of child.
Only the approval admits children: each child's workflow is published first, then the
answer is applied, then each child becomes a paused dependent run. A ticket is scoped
to the repositories it owns; a draft and a feature only plan and own no folder
(`PLANNING_SCOPE`). The rules are pure (`sdd_core.tickets`): a draft's cut covers every
open row exactly once (`covers_rows`), a ticket changes one repository
(`one_repository`), and `after` names existing tickets from the same source.

A task record remembers its `source` (the file or tracker item it came from; children
keep it) and its `labels`. The source is provenance and keeps an import from taking
the same rows twice; nothing else follows it. A feature that depends on another is
told, once, which tickets that one planned, so its own tickets can wait for them.
The cut, the specification and the tickets are stored as artifacts (`ui_artifacts`)
and exported to `<data>/artifacts/<project>/<task>/`; the project repository only
receives code.
The engine finds a product by the step option that declares it (`Engine.outputs`),
not by guessing from result shapes.

Step options are typed (`sdd_core.options.StepOptions`): purpose, produces, recovery
step, auto answer and outcome, preset questions, title, command argv/cwd/error pattern,
profile snapshot. Publication refuses unknown options, so a misspelt option fails
early instead of being ignored at run time.

Every agent result may carry `notes` (0-5 durable facts). They form the task memory
shared by later steps and by other agents, so a fallback profile or a fresh session
continues from recorded decisions instead of re-reading transcripts.

Revival: a run blocked by the core's `WAIT_RETRY_LIMIT` (consecutive infrastructure
waits, usually provider limits) is retried by the running queue after 30 minutes of
rest when its agent profile or a rotation member is available, at most five times
per session and only while the `revive` queue setting is on. Queue budgets apply.

## Console (sdd-ui)

`sdd_ui.service.WorkspaceService` composes the factory, the queue and the agents and
maps HTTP action names to use cases. `queue` owns the coordinator's lifecycle and the
queue settings (stored in the control database), `agents` the profiles file (shared with the CLI's `--config`) and rotation,
`attention` the single "why is this task (not) moving" derivation.

The board's read model is a projection per run with server-derived fields: attention,
lane, dependencies and waits, and the model calls still needed; locations and
dependencies are read in one query each, and the snapshot carries workflows without
their prompts (the editor reads `/api/definition`). The task drawer reads a run in
full. `/api/state` carries an ETag; an unchanged board is answered with 304.

The coordinator only considers runs whose prerequisites are accepted (`runnable`) and
asks `Engine.admissible` (dependencies, slots, claimed paths) before any expensive
observation, so a full queue does not run Git for every waiting ticket. Read-only runs
do not hold their claim between attempts (`machine.holds_claim`), and a read-only agent
step does not wait for unfinished work that holds its paths (`machine.waits_for_holders`):
it waits only for a live process there, so planning and reviews never queue behind
stopped tickets. `Engine.holders` names the work a waiting step waits for, and the board
shows it (`paths_held`). A run the operator paused while it was being dispatched is
skipped, not blocked.

The browser client is a set of ES modules (`static/js`) with keyed dictionaries for
Russian and English; it holds no workflow or acceptance logic and renders the server's
read model instead of deriving task state itself.
