# Changelog

All libraries (`sdd-core`, `sdd-storage`, `sdd-runtime`, `sdd-providers`,
`sdd-workflows`, `sdd-usage`, `sdd-factory`, `sdd-trackers`, `sdd-ui`) and the `feature-factory-ai` product are
released together with one version and pin each other exactly. The engine version
is shown in the UI rail, in **About the engine** and by `ffai --version`.
`tests/test_versions.py` rejects a release where versions or pins diverge.

A version bump follows Semantic Versioning: a breaking change to a published
contract (workflow schema, result schema, storage schema, HTTP actions) needs a new
minor version before 1.0 and a migration; existing tasks stay pinned to the workflow
digest they were created with.

## Unreleased

- **A breakdown can wait for existing work of its plan.** A ticket names run ids of
  the plan's queued or delivered tickets in `after` (`depends_on` stays within the
  breakdown). Approval and plan reviews refuse an `after` that is not a ticket of the
  plan, listing it; the admitted ticket depends on that run.
- **No duplicate planning after a rebuild.** A feature's brief lists the plan's
  delivered tickets beside its queued ones. A started per-row requirement of the
  earlier import whose row a feature now plans is superseded by it: paused, shown as
  **Superseded** in the done column, its recorded work already in the feature.
- **Rebuilding the board plans again everything that never started.** Never-started
  features and tickets (imported or approved under earlier rules) are removed, and
  what they recorded carries into the new feature; a feature whose tickets were removed
  is closed and its rows are planned again, while started tickets keep running.
  `plans-rebuild` takes an optional `plan`. Backups no longer collide within a second.
- **Plan 110 incident: tickets no longer block on upgrades, and work across
  repositories converges.**
  - Handlers are pinned per attempt. An agent CLI update, engine upgrade or profile
    edit applies from the next attempt; while an attempt is live the coordinator waits
    and never blocks. `handler.json` records each attempt's handler. The 11 tickets
    blocked by "Pinned handler settings or version changed" are released once on start.
  - One repository per ticket: work across repositories is a chain of tickets, the
    dependency first. The planner is told this, and approval refuses a spanning ticket.
  - Repository links are a port (`sdd_core.links`) with a git submodule adapter. Lanes
    check linked dependencies out at their pins, so they build without temporary
    projects.
  - A deterministic `commit` step runs after every agent pass in isolated tickets. It
    commits the work with the project's template, advances the pins of dependencies
    that accepted prerequisites delivered, and links a missing dependency the way its
    neighbours are linked. Commits use the repository's configured author; the
    templates are project settings.
  - A ticket that exhausts a step's visits asks the plan lead for a review.
  - A run waiting for a person holds no workspace; `lanes.load_lane` reads its version.

- **A plan lead reviews approved plans.** Ticket T10 was blocked because its
  acceptance needed licensed recordings from the web, which neither the planner knew
  nor the agent could reach. Now:
  - When a ticket's own agent blocks it, and right after a breakdown is approved, a
    `plan-review` run proposes changes: revise, merge, split, cancel, needs, guidance
    and retry. A person approves the proposal in "Needs you"; the agent never changes
    the plan itself. The rules are pure (`sdd_core.plan_changes`). Approved changes
    apply idempotently (`sdd_factory.reviews`), and `Engine.revise` rewrites a
    never-started ticket's brief, paths and prerequisites.
  - Tickets carry typed `needs` (`human`, `asset`, `web`) instead of the `HITL:` goal
    prefix, which is still read. Tickets that need a person or an asset stay out of
    bulk starts.
  - Planners and reviews are told what the ticket agents can do, derived from the
    ticket flow.
  - Step option `tools: ["web"]` grants the internet (Claude WebSearch/WebFetch,
    Codex live search); the project setting **web** applies it to the ticket flow.
  - A run waiting for a person no longer holds its workspace, so a pending approval
    or review does not stop other tickets there.
  - A step product's structured output is declared once in a table
    (`PRODUCT_OUTPUTS`).

- **Subscriptions are paced by their windows, not by counted calls.** Two tasks
  stopped on a fixed 40-call queue cap while both subscriptions had room; 16 of those
  40 calls never reached a model. Now:
  - Claude (account usage endpoint) and Codex (app-server) windows are read on a slow
    schedule without a model turn; a spent window rests its profiles until the reset,
    and the coordinator leaves their steps unstarted instead of spending attempts.
  - The Usage view shows every subscription's remaining share per window, its reset
    time and which agents rest because of it.
  - An attempt that provably reached no model (host ended before the payload
    started, failed preflight, immediate provider refusal) returns its reserved call
    and keeps token accounting known.
  - Absolute reset times ("try again at Oct 4th, 2026 11:41 PM", "resets 3pm") are
    parsed, so a weekly limit is no longer retried every hour.
  - The queue call cap is optional (for API-key agents) and off by default; settings
    holding the old defaults 40/8 are read as no cap.
  - Attempt log names are part of the handler contract (`sdd_core.sdk.STDOUT_LOG`).

- **One execution path.** The coordinator only schedules: every attempt is
  submitted through `ExecutionDriver` to the local `Supervisor` (an
  `ExecutionBackend`) and applied from its observations. The compatibility launch
  path (`Coordinator.start/collect`, `hosts.py`) and the separate
  `ProcessExecutionBackend` are gone; `Coordinator.active()` and
  `Coordinator.collect(now)` replace `live` and per-attempt collection. The storage
  ports drop `claim_host`/`host_started`; an attempt's host identity lives in its
  folder (`identity.json`). Attempts started by an earlier release are reconciled
  once on restore by their recorded host.
- **Sandboxed processes with proof of the end.** A `Sandbox` pairs the OS
  container with a durable lineage of every descendant. The Python install
  manager's `python.exe` alias runs its interpreter in a packaged app's job that
  lets children break away silently; an agent's Godot and helper processes started
  that way belonged to no job, outlived a timed-out attempt and blocked the run as
  "Workspace changed outside attempt". A stop now ends them too, also after a
  coordinator restart; escapes are recorded in `containment.json`, and an end that
  cannot be confirmed keeps the attempt `unknown`.
- Agents get a PATH where the interpreters behind packaged-app aliases come first,
  so their children stay in the job.
- Claude working steps enforce the time budget with a PreToolUse hook
  (`sdd_providers.budget`): background commands are refused in the last quarter,
  every command in the last tenth, each time asking the agent to return its result.
- The driver names why an execution ended from the run itself ("Attempt timeout",
  "Stopped by operator") and settles a synchronous cancel in the same poll.
- **Core refactor.** See `docs/CORE-REVIEW.md`. Storage and HTTP documents keep
  their spelling; runs written earlier load unchanged.
  - Closed vocabularies (kinds, statuses, causes, events, commands, id patterns)
    are declared once in `sdd_core.models`; one strict `codec.decode`/`encode`
    pair replaces the hand-written loaders and every `asdict(run)`.
  - **Typed hold cause.** `Run.cause` decides behaviour (stop requests, call-limit
    grants, revival after `wait_limit`, the board's `uncertain` state); `reason` is
    display text only. Older runs derive their cause from their reason on load.
  - `Run.spend` (`Spend`) groups calls, planning calls, granted calls, tokens and
    unknown usage; the stored document stays flat.
  - Every transition is checked against `STATUS_CHANGES` and the status, attempt
    and cause invariants. `accepted` is final: `stop` on an accepted task is
    refused and the coordinator no longer blocks one. `resume` withdraws a pending
    stop request, so "Stop requested" no longer lingers as the visible reason.
  - `ApplicationEngine` became a facade over `RunCommands`, `Scheduler`,
    `ResultIntake` and `HumanAnswers`; slot and queue-budget rules
    (`sdd_core.admission`) and recovery-route rules moved into the core.
  - `sdd_core.runtime_ports` was removed (`UnitOfWork` lists the record roles);
    `sdd_core.wire` holds JSON primitives, `sdd_core.tickets` ticket drafts.
  - `tests/test_simulation.py`: seeded deterministic simulation of commands,
    results, faults and restarts with invariant checks after every step.

## 0.4.0 — 2026-09-29

- Work is a tree of any depth: a parent (a feature, or a ticket split further) is
  no longer done when its specification is approved. Its state and progress come
  from its children: paused, partly done, delivering, needs you, delivered. The
  state read model adds `progress` per parent and seven attention reasons.
- **Закрыть частично** (`close` action) counts a partly done parent as delivered
  and detaches its unfinished children as top-level work that remembers its
  `origin`; task records gain `closed` and `origin`.
- The board opens on a **tree** of features, tickets and sub-tickets with
  progress, **Доделать** and lane focus chips instead of kind chips; **Канбан**
  and **По планам** show approved parents through their tickets.
- The approved specification is kept whole: it was cut at 20 000 characters
  mid-word in the feature's artifact, its `spec.md` and every ticket's brief. A
  ticket's brief now fits its workflow's input budget, keeps 8 000 characters for
  task memory, and says when and how much of the specification it shortened.
- Ticket briefs are built before an approval applies, so an oversized ticket is
  refused while the feature still awaits approval. An approval interrupted
  between accepting the feature and admitting its tickets is finished on the next
  start (`tickets_readmitted` in the flight log).
- A run's pinned handler manifest follows the operator's current profiles until
  the run dispatches its first process attempt. Tickets admitted before a profile
  or rotation change were blocked with "Pinned handler settings or version
  changed" without ever running; runs that already executed keep their pin.
- Agent prompts reach the Claude and Codex CLIs on stdin instead of the command
  line. A ticket brief with its whole specification (~50 000 characters) passed
  the 32 767-character Windows limit, so the host could not start the agent and
  the attempt ended as "Host exited without durable completion". `Launch.input`
  carries the text; the coordinator refuses an over-long command line before
  launch with a clear reason.
- Recovery reconciles only what an attempt could have changed. A lost attempt
  that left the workspace at its base revision retries the same step instead of
  going to `reconcile`, and **Reconcile and recover** sends a run whose mutating
  attempts changed nothing back to its first mutating step with a fresh retry
  budget. Tickets whose launches never started were routed through reconcile and
  diagnose, and a diagnosis of the old launch failure blocked them.
- Claude may run shell commands on mutating steps (`--allowedTools
  Bash,PowerShell`). `acceptEdits` approves file edits only, so a non-interactive
  implement or repair step had every test, check and `git status` refused and
  could deliver code without running a single check. Read-only steps still deny
  commands and edits; project hooks keep applying.
- **Retry** on a run stopped by "Model call limit" grants it the workflow's
  `max_calls` once more (`calls_granted` in the journal; runs record
  `granted_calls`). Before, the limit had no operator remedy: a retry dispatched
  and hit the same limit. The queue's call budget still applies.
- Tickets show their place in the plan: number and wave (`T15 · wave 3`: it
  starts once the tickets it depends on are accepted), "after T12, T20" while it
  waits, and a "needs a person" mark for HITL tickets. The tree lists a parent's
  tickets by wave, then number. The state read model adds `ticket` per run.
- Resuming many tasks leaves HITL tickets paused unless they are named by id;
  the dialog says how many were held and the result lists them in `held`.
- **Trackers** ([docs/TRACKERS.md](docs/TRACKERS.md)): a project's work can come
  from a tracker instead of plan files, and the factory mirrors the approved
  specification, the approved tickets (as issues with "blocks" relations) and
  every item's state back to it. Neutral contracts live in `sdd_core.tracking`
  (`WorkItem`, `WorkSource`, `Tracker`, `TrackerUpdate`); Markdown plans are one
  `WorkSource`. Publications are recorded in a `tracker_outbox` (schema version 5)
  before delivery and delivered in order on their own thread, with growing
  retries. Adapters register under `sdd.trackers`; settings never store a token
  (`token_env` names the variable). The new `sdd-trackers` library ships the
  Linear adapter, covered by a recorded conversation, not yet qualified against a
  live workspace. Project settings choose the tracker.
- `install.py` installs `sdd-usage` and `sdd-factory` from the checkout too; before,
  a plain-pip installation looked for them on the package index.
- Hosts, execution hosts and the supervised coordinator start suspended on Windows
  and run only once every job holds them (`start_contained`). A venv `python.exe`
  launcher used to start the real interpreter before assignment; under a parent job
  that allows silent breakaway, the whole agent tree then stayed outside the
  attempt's job, outlived a confirmed stop and changed the workspace afterwards,
  blocking the run with "Workspace changed outside attempt".
- Agent prompts state the attempt's time budget: a stopped step returns no result,
  so agents keep commands well inside it and leave long verification to check steps.

## 0.3.1 — 2026-09-28

- The orchestrator assumes no plan layout: a project names its **plans folder**
  (project settings; empty means no plans), and the legacy import derives it from
  the imported plan paths. Requirements plan in the folder of their own plan file.

## 0.3.0 — 2026-09-28

Breaking (pre-1.0 minor): storage schema 4, the workflow template names and the
task kind vocabulary changed; existing databases migrate on open after a backup,
and existing tasks keep their pinned workflow digests.

- New `sdd-factory` application layer: typed task records (feature, ticket, task)
  that can be corrected, the project catalog with checks per repository, feature
  and ticket use cases, plan sources and the legacy import
  (`python -m sdd_factory.legacy`); `sdd-ui` is the console over it.
- The input is a feature: a specification (PRD) and a breakdown into vertical
  slices, approved once; a plan file becomes one feature over its open rows, new
  rows become follow-up features, and the board can be rebuilt from plans with a
  backup. Specifications and tickets are stored as feature artifacts and exported
  by the factory, never written into the project.
- Templates: `feature` is specification → tickets → approval; the former
  `feature` is `approved-feature`. Intents are `feature`, `main-flow`, `ticket`.
- Typed step options (`sdd_core.options.StepOptions`); publication refuses unknown
  options; steps declare what they produce (`produces: specification | tickets`).
- Core: a task paused during its own dispatch is skipped instead of blocked; only
  a run that dispatched a mutating step holds its paths between attempts; the
  coordinator checks slots and claims before lanes and Git and only considers runs
  whose prerequisites are accepted.
- Queue settings live in the control database; the console restarts itself
  (keeping a running queue running) and offers a restart when newer code is
  installed.
- Board: status and plan views, column paging, plan start with outside
  dependencies and call estimates, dependencies in the task drawer, feature
  documents; the board snapshot is projected and derived on the server.
- Front end split into `js/board`, `js/drawer` and `js/flows` modules.

## 0.2.0 — 2026-09-28

- Operator console rebuilt as ES modules with keyed Russian/English copy, dialogs
  and drawers that close by button, Escape and backdrop, and drafts for every form.
- Tasks are created by kind; a requirement's approved breakdown creates paused
  child ticket tasks with their dependencies.
- One attention reason per task, bulk resume/pause through an explaining dialog,
  and a queue start that explains when nothing is resumed.
- Live progress of the running step: elapsed time, timeout, output freshness and
  the output tail; running cards show elapsed time.
- Notification center (needs you now, events since last read, optional system
  notifications), flight log and per-task incident export.
- Task memory: agents declare durable notes; packets carry a compact brief
  instead of raw receipts.
- Automatic revival of tasks blocked by repeated limit waits.
- Library versions reported by the server and shown in the UI.
- Overview dashboard as the default screen; the team office moved into it. The
  board groups the queue by what can start, scrolls lanes sideways on narrow
  screens, and connected agents hide their connection form.

## 0.1.0 — 2026-09-27

- Deterministic engine, local runtime, providers, workflows, usage pricing and
  the first local UI.
