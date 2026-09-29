# Core review and refactoring plan (2026-09-29)

Scope: `sdd_core` (≈2.3k lines) and `sdd_runtime.application`, the service that
drives it. References: Orca (worktree-per-agent orchestration) and
mobile-sdd-factory (Constellation) were read for comparison; no code was copied.

## What is already right

- **Functional core, imperative shell.** `machine` is a set of pure functions
  `(state, input, now, ids) -> Transition(state', events, effects)`. Time and
  identifiers are injected; `tools/check_boundaries.py` forbids IO modules in core
  and `machine.changed` outside it.
- **Transactional outbox and CAS.** A transition's state, journal and effects are
  written in one transaction against the version that was read; slow observations
  (Git, hashing) happen outside and the write retries on `StaleVersion`.
- **Idempotency everywhere it matters.** Operator commands by request id (reuse
  with other input fails); results by attempt id plus digest (a conflicting receipt
  fails).
- **Honest failure semantics.** Unknown process ownership blocks and is never
  killed or retried by guess; no exactly-once claim for external side effects.
- **Pinned, content-addressed workflows.** A run executes the definition digest it
  was created with — the same answer Temporal gives to workflow versioning.
- **Graph validation beyond syntax.** Reachability, "required step cannot be
  bypassed" and "every component can finish" are checked on publication.

Constellation shows the contrast: a declarative `ALLOWED_TRANSITIONS` table (worth
copying) next to an 11,989-line `coordinator/service.py` (worth avoiding).

## Findings

| # | Priority | Finding | Status |
|---|---|---|---|
| 1 | High | Closed vocabularies spelled in several places: step kinds in `Kind`, `graph`, `schema`; id patterns in `graph`, `schema`, `machine`, `application`; event kinds and commands were free `str`. | **Done**: `KINDS`, `STATUSES`, `COMMANDS`, `EventKind`, `STEP_ID`, `RECORD_ID`, `PROCESS_KINDS`, `RESERVED_OUTCOMES` in `models`. |
| 2 | High | `codec` hand-wrote a loader per model, repeating every default (`max_visits=3`, `timeout=900`, `max_calls=10`…) and not validating `Status`. | **Done**: one strict `decode` driven by dataclass hints; defaults declared once; `Literal` values validated; closed/open classes explicit. |
| 3 | High | Domain rules lived in `ApplicationEngine`: which step a lost attempt resumes at, "nothing changed yet → restart", human-answer results, slot counting, queue budget. | **Done**: `machine.recovery_route/recovery_target/first_untouched/human_result`, `admission.Slots/QueueBudget`. |
| 4 | High | Run invariants were implicit (a `ready` run with a live attempt was representable). `dispatch` built its `Transition` by hand, duplicating the version bump. | **Done**: every transition goes through `changed`, which checks status ⇄ attempt consistency. |
| 5 | High | **Human-readable `reason` doubles as a state discriminator**: `stop_requested` compares `reason == "Stop requested"`, retry compares `reason == "Model call limit"`, the UI matches `WAIT_RETRY_LIMIT`. Rewording a message changes behaviour. | **Done**: typed `Run.cause`; behaviour and the UI read it, `reason` is display text; older runs derive it on load. |
| 6 | Medium | `Run` is a flat record of 24 fields; budget counters (`calls`, `planning_calls`, `granted_calls`, `tokens`, `usage_unknown`) and retry state (`infrastructure_failures`, `wake_at`) are unrelated concerns in one bag. | **Done** for model use: `Run.spend` (`Spend.reserve/measure/grant`); the wire stays flat because SQL reads `$.calls`. The timer (`wake_at`) and retry counter stay flat on `Run`: one field each does not earn a value object. |
| 7 | Medium | JSON strings inside typed models: `Step.config`, `Result.data`, event `detail`. Every reader reparses (an LRU cache hides the cost). | Partly: `step.options` is the single typed view; storage stays canonical JSON. |
| 8 | Medium | `ApplicationEngine` is a 20-method service mixing commands, queries (`facts`, `outputs`, `asked`) and human answers; the "observe outside, CAS inside, retry" loop is written twice (`dispatch`, `complete`). | **Done**: `RunCommands`, `Scheduler`, `ResultIntake`, `HumanAnswers` over one `RunContext`; `ApplicationEngine` is their facade; one `optimistic()` CAS loop. |
| 9 | Medium | `memory` mixed task memory with ticket decomposition; `portfolio.ordered` and `tickets_of` each implemented Kahn's algorithm. | **Done**: `sdd_core.tickets`, shared `graph.dependency_layers`. |
| 10 | Low | JSON primitives lived in `codec`, which imports models, so `models` could not use them without a cycle. | **Done**: `sdd_core.wire`. |
| 11 | Low | Ports: `RuntimeRecords` redeclares `recent_results`/`set_policy`; `StateStore.get` duplicates `unit().run`; `records.py` holds the domain constant `UNPINNED_KINDS`. | **Done**: `runtime_ports.py` removed, `UnitOfWork` lists the roles once, `UNPINNED_KINDS` beside the kinds. `StateStore.get` stays as the documented snapshot read outside a transaction. |
| 12 | Low | `TrackerUpdate.id` and `update_json` serialize the same fields twice. The id must stay stable for outbox de-duplication, so only a shared field list is safe. | **Done**: `id`, `update_json` and `update_load` derive from `codec.encode/decode`; ids are byte-identical to 0.4.0. |

## How deterministic engines are built — and what applies here

- **Temporal / Cadence**: workflow code is replayed against an event history; the
  code must be deterministic, side effects are *activities*, decisions are
  *commands*, and code changes need explicit versioning. Here: attempts are
  activities, `Effect`s are commands, and the pinned digest is the versioning.
  The difference is that we store the state snapshot, not only the events.
- **Decider pattern** (Chassaing): `decide(command, state) -> events`,
  `evolve(state, event) -> state`. It makes the event the unit of truth and
  replay free. Our `Transition` is a "decide + evolve fused" variant; typed event
  payloads (plan step 5) would let the journal become the source of truth later.
- **AWS Step Functions (ASL)**: a declarative graph with `Choice` states and
  per-state `Retry`/`Catch` with backoff — the same shape as our condition steps
  and infrastructure retries. ASL keeps retry policy *in the definition*; ours is a
  constant (`INFRASTRUCTURE_RETRIES`) and could become a workflow field.
- **Statecharts / SCXML / XState**: states, guards and actions as data make every
  transition enumerable, drawable and model-testable. The cheap version for us is
  Constellation's idea: an explicit status transition table checked by `changed`.
- **Restate / DBOS**: journaled durable execution on a database, with steps
  recorded before they run — the transactional outbox we already have.
- **Deterministic simulation testing** (FoundationDB, TigerBeetle VOPR): with
  clock, randomness and IO injected, a seeded simulator runs thousands of
  schedules with crashes and faults and replays any failure from its seed. The
  core is already pure enough; the missing piece is one seeded driver over
  `ApplicationEngine` + `MemoryStore` + a fake `ExecutionBackend`.

## Plan — completed

1. **Typed hold cause.** `Run.cause` (`models.Cause`) decides behaviour: stop,
   call-limit grants, revival of `wait_limit`, the board's `uncertain` state. A
   blocked run always has a cause and a reason (checked in `changed`).
2. **Value object for model use.** `Run.spend: Spend`; `codec` keeps the flat wire
   through `FLAT` field metadata, and every outside spelling (storage, HTTP, CLI)
   goes through `codec.encode`.
3. **`ApplicationEngine` split** into `sdd_runtime.commands`, `.scheduling`,
   `.intake`, `.answers` over `.runs.RunContext`, with the facade keeping its API.
4. **Port cleanup** as in finding 11.
5. **Status transition table.** `models.STATUS_CHANGES`, asserted for every
   transition; `accepted` is final (the coordinator no longer tries to block an
   accepted run). Event payloads stay text: the journal only displays `detail`, so
   typed payload classes would have no reader (KISS).
6. **Seeded simulator** (`tests/test_simulation.py`): 40 seeds × 300 steps of
   commands, results, repeated deliveries, lost hosts, outside edits, process
   restarts and clock jumps over the memory store. After every step it checks the
   status/attempt/cause invariants, slot limits, one claim holder per workspace,
   the queue budget, idempotent redelivery and that accepted runs stay accepted.
   It found two rules worth fixing: a stop recorded on an accepted run, and a
   resumed run keeping "Stop requested" as its visible reason.
