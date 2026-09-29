# Changelog

All libraries (`sdd-core`, `sdd-storage`, `sdd-runtime`, `sdd-providers`,
`sdd-workflows`, `sdd-usage`, `sdd-factory`, `sdd-ui`) and the `feature-factory-ai` product are
released together with one version and pin each other exactly. The engine version
is shown in the UI rail, in **About the engine** and by `ffai --version`.
`tests/test_versions.py` rejects a release where versions or pins diverge.

A version bump follows Semantic Versioning: a breaking change to a published
contract (workflow schema, result schema, storage schema, HTTP actions) needs a new
minor version before 1.0 and a migration; existing tasks stay pinned to the workflow
digest they were created with.

## Unreleased

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
