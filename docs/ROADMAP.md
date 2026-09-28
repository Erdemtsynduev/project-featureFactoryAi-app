# Delivery stages

## Current refactor: independent kernel only, no migration

The active scope includes execution contracts, named agent profiles, SQL-free
application ports and the test matrix. Agent discovery/profile composition is
implemented; see [AGENT-PROFILES.md](AGENT-PROFILES.md). See
[EXECUTION-CONTRACT.md](EXECUTION-CONTRACT.md) and
[REFACTOR-VERIFICATION.md](REFACTOR-VERIFICATION.md). Application migration below is
historical future roadmap, not authorized current work. POSIX support is implemented
but awaits real OS qualification; Windows-only assumptions below describe the baseline.

## Engine foundation: implemented, long-duration qualification pending

- Five independently installable libraries and umbrella distribution.
- Typed pure graph/state machine; editor operations, JSON schema, CLI and templates.
- Atomic SQLite state/events/outbox, idempotent receipts, CAS commands and backups/migration.
- Windows Jobs, gated spawn, durable host completion, reconciliation, coordinator and supervisor.
- Separate installed example handler/provider and injectable project adapter.
- Fresh Claude/Codex adapters; one successful live protocol call for each.
- Context/portfolio primitives, project policy gates, Git worktree and fast-forward integration API.
- Static/contract/process/fault tests, Windows CI and isolated wheel installation check.

The 24-hour soak is a separate qualification gate. Read `reports/soak-24h/progress.json`;
only `status: passed` with at least 86400 elapsed seconds satisfies it.

## Remaining before claiming full legacy Main Flow parity

- Port the legacy immutable specification/ticket preparation and admission contracts into explicit
  activity artifacts. The current graph template preserves stage ordering but is not a full import
  of the legacy portfolio service or every preparation/repair branch.
- Connect the old portfolio application's approved ticket output to `PortfolioService.admit`.
  The service already creates immutable, paused, dependent runs idempotently and verifies requirement
  closure. The application-specific legacy import is still a later integration task.
- Qualify subscription reset parsing against actual provider error payloads. Unknown CLI errors
  currently block; bounded waiting exists in the result protocol.
- Run a bounded real feature through specification, implementation, checks and independent review.
  The live tests executed here qualify CLI protocols only.

These gaps do not justify migrating a production queue yet.

## After core qualification

1. **Current application adapter.** Expose Engine commands/projections through the current panel's
   API. Test one isolated project; remove duplicate dispatch decisions from the client/server glue.
2. **Visual Main Flow editor.** Nodes, edges, prompt/config forms, mandatory steps, model profiles,
   budgets, validation feedback, graph diff and publish. Use the exported schema/SDK catalog;
   layout changes never affect workflow digest. Existing runs remain pinned.
3. **One-project migration.** Drain/reconcile active work, back up database and artifacts, import
   requirement/task IDs, dependencies and evidence. Enforce one queue owner. Historical completion
   markers do not become fresh acceptance.
4. **Remaining projects.** Expand only after pilot evidence. Rollback restores a compatible state
   snapshot and reconciles newer code; never overwrite new commits with an old database assumption.
5. **Reusable flow library.** Feature, bugfix, research, maintenance and release-preparation packages
   with versioned prompts/contracts and compatibility tests.
6. **Measured optimization.** Compare model profiles/context sizes/stages on identical bounded tasks;
   measure tokens per accepted change, retries, interventions and missed defects together.

Later, separately qualified work: ORCA adapter, external trackers, Linux/remote hosts, parallel
branches and joins, then reviewed self-improvement in an isolated candidate/eval/rollback cycle.
The running engine never edits itself while owning a production queue.
