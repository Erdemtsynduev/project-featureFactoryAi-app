# Refactor verification, 2026-09-27



This report distinguishes implemented behavior, executed checks and unqualified work.

No production database, queue, UI or orchestrator code was changed.



## Executed on Windows / Python 3.14



- Core purity/import boundary gate, Ruff and strict mypy.

- Additional static mypy checks targeting Linux and macOS. These are not OS execution tests.

- Full final suite: **91 passed in 18.53 seconds**, recorded in

  `reports/refactor-qualification/tests.xml` and `tests.log`.

- Hypothesis generated lifecycle traces (150 examples, up to 100 actions), bounded

  loop cases and generated dependency DAGs. `.hypothesis` retains shrunk failures;

  deterministic lifecycle generation is reproducible.

- Real loopback HTTP contract: duplicate request, lost ownership, cancellation,

  late receipt, stale generation, restart and separation from application-specific coordinator.

- Real local command through the neutral driver: success/failure, spaces/Unicode,

  gate acceptance and duplicate submit.

- External installed executor using only the public core package.

- Windows host/coordinator/supervisor termination, evidence tampering, atomic rollback,

  duplicate receipts, resource claims and two parallel attempts from the existing suite.

- Checkpoint-only rejection; exact current checkpoint exclusion, including normalized

  aliases; failed gate cannot reuse a previous successful verdict.

- Synthetic Cursor/OpenCode protocol matrices. Cursor real smoke passed;

  OpenCode exited 0 after 40.3 seconds but its adapter rejected the response; it remains unqualified. See `reports/refactor-live`.

- Wheel build and clean non-editable installation: `reports/refactor-qualification/wheels.log`.

  All eight local qualification commands passed, with unchanged source fingerprint:

  `84f7f48081ec54a374ef9910c34cf8aa12eba783f18264ca661615459680ed83`.

  Reproduce with `python tools/qualify.py --build`.



## Duration evidence



The old Windows soak stopped on its source-fingerprint guard after approximately

2146 seconds and 134 tasks. It did not pass 24 hours and cannot qualify this revision.



The new `tools/soak.py` defaults to two independent workspaces, exercises two agent

slots with deterministic command providers, injects owned-host termination, checks

replay, and records maximum observed parallelism. It makes no model calls. Its

short run is only a harness check; a 24-hour PASS requires `elapsed >= 86400` for

the same source fingerprint. The final short run passed: 20 tasks, one injected

host failure recovered, two simultaneously observed executors, 12.208 seconds,

zero model calls (`reports/refactor-short-final/progress.json`).



The previous Windows 24-hour run started at 2026-09-27 20:06 UTC, PID 7264,

and stopped on the source-change guard after 128 tasks and about 1032 seconds.

It is invalidated, not passed. Inspect `reports/refactor-soak-24h/progress.json` and

`launch.json`; check the process identity before any later process-control operation.



## Not qualified / remaining work



- Real Linux/macOS execution and 24-hour qualification on all three OSes.

- POSIX process-group escape, machine power loss and constrained-filesystem ENOSPC.

  SQLite failure injection proves rollback, not physical disk exhaustion.

- OpenCode new live end-to-end qualification after the parser correction, read-only

  enforcement and subscription reset parsing; Cursor/OpenCode normalized usage and resumed sessions.

- Authenticated durable remote executor service and remote workspace/evidence transport.

- Full application-specific Main Flow preparation/reconciliation parity and historical checkpoint

  recovery fixture port. The running application's fixes remain intact there.

- Terminal attach, replacing SQLite-specific coordinator/execution projections, and

  additional durable StateStore/workspace implementations. Application use cases now

  depend on ports and have SQLite/memory contract tests.



The foundation is testable and extensible, but the entire qualification plan is not

complete. No migration is authorized by these results.



## OpenCode diagnosis correction



Read-only inspection of the saved attempt on 2026-09-27 disproved the initial

timeout diagnosis. Dispatch: 1790538777.0471067; deadline: 1790538897.0471067;

host completion: 1790538817.348063, exit_code 0. The recovery event explicitly

says `OpenCode stream ended without confirmed completion`. Output contains the

requested JSON inside a Markdown fence, without a final step_finish(reason=stop).

The subsequent `Model call limit` is a secondary stop preventing another model

call. The parser and error classification were corrected in the profile refactor below;

increasing the timeout is not the fix. Original captured logs were preserved.





## Profile/application refactor qualification — 2026-09-27



Current source fingerprint:

`89b0ddf1b0397f3c66426d968ba4c12e14361eeb1eae018af96540ec5f78d8b3`.

`reports/profiles-qualification/report.json`: all checks PASS, source unchanged.

119 tests pass, including two-store application contracts, profile immutability,

permission rejection, explicit plugin loading, catalog formats, captured OpenCode

2.0.16 replay and restart preservation of protocol errors without a second call.

Strict mypy now includes 52 modules across libraries, tools and example extension.

Ruff/format and dependency gates pass; isolated wheel installation passes.

Linux/macOS mypy configurations passed **static checks only**.



`reports/profiles-short/progress.json`: 22 completed tasks, two injected host failures

recovered, two observed simultaneous executors, 17.825 seconds, zero model calls.

The new 24-hour Windows run started 2026-09-27 20:46:14 UTC, PID 1704.

`reports/profiles-soak-24h/launch.json` records identity;

`reports/profiles-soak-24h/progress.json` records progress. It is **pending**, not

passed. This run was subsequently invalidated by the storage-port refactor after 40 tasks,
3 recoveries and 317 seconds. It did not pass 24 hours.



Discovery was exercised against installed Cursor versions and correctly reported

ambiguity. Cursor's native model catalog returned identifiers; OpenCode 2.0.16's

catalog returned empty stdout with exit 0. The adapter now reports unknown catalog

availability. No new paid agent execution was needed for these checks. Read

[AGENT-PROFILES.md](AGENT-PROFILES.md) for exact configuration and limitations.



Next gates: finish the unchanged-source 24-hour run; execute the OS fault matrix

on real Linux/macOS; qualify chosen model/profile combinations with bounded live

calls and usage/subscription handling. Only then evaluate an isolated application

adapter and one-project pilot. The production factory and queue remain untouched.



## Runtime storage replacement qualification — 2026-09-27

Current source: `782ff2b5afd7d311609e8c00d2454eae7103d91a169567b8b5781173076d50a9`.
`reports/storage-ports-qualification/report.json`: 135 tests, Ruff/format,
boundary gate, strict mypy (Windows/Linux/macOS configurations) and isolated wheel
installation PASS with unchanged source. Type checks are not real Linux/macOS runs.
The wheel check first installs runtime without storage and imports application,
coordinator, execution driver, portfolio and maintenance; then checks full CLI composition.

Sixteen runtime contract cases cover both SQLite and independent MemoryStore:
real child-process acceptance, durable-exit reconciliation without relaunch,
concurrent ownership arbitration, host nonce checking, rollback, immutable bindings,
external unknown/terminated observations and result projections. MemoryStore is
volatile; coordinator reconstruction using the same object is not persistence proof.
Existing SQLite restart/backup and execution fault tests remain in the full suite.

Short run: 24 tasks, 2 recovered injected host failures, 2 simultaneous executors,
15.099 seconds, zero model calls (`reports/storage-ports-short/progress.json`).
New Windows 24-hour run started 2026-09-27 20:59:51 UTC, PID 29336;
`reports/storage-ports-soak-24h/launch.json` records identity and
`progress.json` records progress. Status is pending, not passed.
The prior profile soak stopped on the source guard and cannot qualify this version.

Replacement contracts, composition example and backend limitations:
[REPLACING-MODULES.md](REPLACING-MODULES.md). No production factory/queue migration.


## User-requested early stop — 2026-09-28

The storage-port soak was stopped at an idle boundary on user request after
35,205.77 seconds (about 9 hours 47 minutes). Final status: `stopped_by_user`,
not a 24-hour PASS. All owned launcher/runner/console processes were terminated;
there were no active effects or unfinished runs at the stop boundary.

Post-stop audit (`reports/storage-ports-soak-24h/stop-audit.json`):
3,702 runs accepted; 3,702 passing results; SQLite integrity `ok`; every journal
replays to its saved projection; zero duplicate attempt generations; no invalid
active/gate state at acceptance. All 7,404 referenced evidence files match their
hashes. stderr is empty. Maximum simultaneous executors: 2. Model calls: 0.
The harness records 264 termination injections; 120 effects are abandoned and
replaced, while injections need not always discard an already completed result.
These are different counters, not 264 proven reexecutions.

The original final progress snapshot is preserved in `progress-before-stop.json`;
`stop.json` records process identities and the idle boundary. No soak was restarted.
This is useful Windows deterministic-runner endurance evidence, not real-model,
Linux/macOS or 24-hour qualification.


## Product naming and installation — 2026-09-28

Product: Feature Factory AI; distribution: `feature-factory-ai`; only public console
command: `ffai`. The repository moved to
`C:/Dev/Projects/AiProjects/feature-factory-ai`. Historical evidence contains paths
under `sdd-engine`; it was retained as captured, not rewritten to imply new runs.
Python module names and plugin groups `sdd_*` / `sdd.*` are library contracts.
No `sdd` or `ff` command alias is installed.

Primary source setup is `uv sync --locked` / `uv run --locked ffai ...` using the
workspace lockfile. Wheels install through standard pip, pipx or uv tool workflows.
`uv tool install --find-links ./dist feature-factory-ai==0.1.0` was exercised locally,
as was `ffai demo` from outside the repository. Installer `install.py` is an optional
stdlib venv/pip bootstrap, not the required installation mechanism.

`reports/ffai-qualification/report.json` records tests, strict types, style,
architecture checks and isolated wheel installation. The build check verifies
`ffai --version`, an isolated no-model demo, and absence of old `sdd`/`ff` launchers.
Windows execution was verified; Linux/macOS installation/runtime remains pending
real CI execution. No long-duration test was restarted and no model was called.


## Local UI and bounded planning (2026-09-28)

The optional `sdd-ui` package adds a loopback operator panel, visual step/transition
editor, versioned human decisions, profile forms and persistent queue call caps.
Core/runtime imports remain independent of UI. The default feature flow consumes
no model planning calls; deep preparation and interview have separate planning
ceilings. No production queue was imported or started.

Regression coverage includes shared call admission across Memory/SQLite engines,
UTF-8 subprocess output, bounded atomic replacement, Windows job membership/dead
process races, stale human answers and stop while queue dispatch is paused.
A real HTTP test runs a check-only task to acceptance. The isolated wheel check
also launches the installed UI command, fetches its assets and closes it cleanly.
No model is invoked by these checks.

Local evidence: `reports/ui-planning-qualification/report.json` and its test/build
logs. Browser rendering was not inspected because no automation browser was
available. Linux/macOS checks are static type checks, not execution qualification.
The earlier 9h47 soak applies to its recorded source fingerprint, not this change.
See [OPERATIONS-LESSONS.md](OPERATIONS-LESSONS.md) for confidence and remaining
production-fault scenarios.
