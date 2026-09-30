# Feature Factory AI

**Deterministic feature workflows. Short command: `ffai`.**

Feature Factory AI runs explicit graphs for specification, implementation, checks,
review and acceptance. Code controls ordering, dependencies, retries and acceptance.
Models run only at steps assigned to them. The independent Python engine can be
embedded in other applications; storage, executors and handlers have replaceable
interfaces.

## Install on Windows, Linux or macOS

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```text
uv sync --locked --all-packages --extra dev
uv run --locked ffai --help
uv run --locked ffai demo
uv run --locked ffai-app
```

Python 3.12+ is required, along with Git for Git workspaces. `uv` creates an isolated
virtual environment and installs locked dependencies. Installation does not start
queues. Node.js and tmux are not required by the engine or UI; individual agent CLIs
may have their own runtime dependencies.

To install the commands globally from locally built wheels (the script clears stale
`build/` trees first, so deleted modules never ship):

```text
uv run --locked python tools/wheels.py
uv tool install --find-links ./dist feature-factory-ai==0.4.0
ffai-app
```

Run `uv tool update-shell` and open a new terminal if the command directory is not
on PATH. Update with `uv tool install --reinstall --find-links ./dist feature-factory-ai==0.4.0`;
uninstall with `uv tool uninstall feature-factory-ai`. Alternatively use
`pipx install --pip-args="--find-links /absolute/path/to/dist" feature-factory-ai==0.4.0`.
The application is not published on PyPI: use local wheels, not an unrelated
package with a similar name. The legacy `ff` and `sdd` commands are not installed.

Without uv, `python install.py` installs through standard `venv` and `pip`.
Then use `python ffai.py --help` or the virtual environment's `ffai` command.
Run `python install.py --help` for installation options.

## Visual workspace

Launch **Feature Factory AI** from its desktop shortcut or run **`ffai-app`**.
The browser opens automatically; subsequent launches reuse the running application.
State lives in the user's application data directory. For a separate control database:

```text
ffai --database /absolute/path/to/control/ui.db ui --port 8791
```

Keep the control database outside task workspaces. The local interface includes:

- A welcome screen until agents and a project exist; the board, team and new-task
  button belong to the selected project.
- A **New task** dialog that asks what kind of work it is — a large feature
  (specification → questions → tickets → your approval), a whole task in one run,
  a ready ticket or a custom published workflow. The project's version of the flow
  is published on first use; drafts save as you type.
- A board by what each task needs (Queue, In progress, Needs you, Done). Every card
  says why the task is or is not moving and offers the one action that moves it.
- Approving a requirement's breakdown creates its tickets as paused child tasks
  with their dependencies and the approved specification; the tree view shows them.
- A task drawer with discussion, questions with recommended answers, messages to
  the next step, task memory, lanes, the engine journal and a downloadable incident.
- A workflow editor whose inspector applies changes immediately and autosaves the
  draft; publication stays explicit and existing tasks keep their version.
- Agents: discovery and sign-in checks without model calls, named profiles,
  rotation on limits and an early "return now" for a resting profile.
- Usage and limits: queue budgets, per-profile tokens and API-equivalent cost,
  daily dispatches, Codex account quotas and automatic revival after limits.
- A flight log of operator actions, queue stops, blocks, waits and revivals, and a
  notification center with what needs you and what happened.
- Bulk resume/pause through a dialog, live progress of the running step (elapsed
  time, timeout, output tail) and the engine version with every library version.
- The pixel office (Team), light/dark/system themes, Russian/English and optional
  browser notifications.

New tasks start paused unless queued from the dialog, and the queue starts paused.
Closing the tab leaves the server running; **Quit application** stops it when no
executions are active. Restarting the UI leaves the queue paused.

Messages submitted to a running task are recorded immediately and included in its
next invocation. They do not inject text into an already running CLI process.
Human-step answers resolve the waiting attempt through the coordinator.

The default main flow contains detailed prompts adapted from the previous
orchestrator's AI Hero workflow: specification, bounded tickets, implementation,
checks, review, diagnosis, repair and reconciliation. Recovery rechecks unfinished
work before returning to checks and review. It cannot mark a card accepted by itself.
The legacy autonomous portfolio scheduler is not silently launched or migrated.

[Changelog](CHANGELOG.md) · [UI guide](docs/UI.md) · [Trackers](docs/TRACKERS.md) · [Legacy feature comparison](docs/LEGACY-PARITY.md) ·
[Operational lessons](docs/OPERATIONS-LESSONS.md).

## Agent profiles

```text
ffai agents discover
ffai agents discover --adapter cursor
ffai models list --runner local-opencode --config profiles.json
ffai profiles validate --config profiles.json --workflow flow.json
```

Installation discovery, authentication and successful execution are separate checks.
Select an explicit executable when multiple installations are found. Native CLI
login remains with the CLI; credentials are not copied into workflow documents.
OpenCode uses an exact `provider/model` identifier. Discovery and model catalog
commands do not call a model. See [Agent profiles](docs/AGENT-PROFILES.md).

## Run an approved feature

Generate the workflow and schema:

```text
ffai template feature --output flow.json
ffai schema > workflow.schema.json
```

Configure agent handlers or named profiles, prompts and a real project check command.
A named-profile step has an empty `handler`. A step's `config` is a JSON string;
for checks, for example:
`{"argv":["C:/my-project/.venv/Scripts/python.exe","-m","pytest","-q"]}`.
Templates do not guess a project's tools or requirements.

PowerShell example (replace `ffai` with `uv run --locked ffai` when using source):

```powershell
ffai validate flow.json --config profiles.json --mandatory checks review
ffai --database .state/features.db policy C:/my-project --mandatory checks review
$definition = ffai --database .state/features.db publish flow.json --config profiles.json --mandatory checks review
ffai --database .state/features.db create $definition C:/my-project --id feature-one --context "Approved requirement" --config profiles.json
ffai --database .state/features.db resume feature-one --version 0
ffai --database .state/features.db start --config profiles.json
```

On POSIX shells, capture the digest with `definition=$(ffai ... publish flow.json ...)`.
`create` leaves tasks paused. `start` is an alias for `supervise` and stays in the
foreground; it does not install an OS startup service. Use `run --once` for one
coordinator pass. Validate workflows on isolated test projects first.

The approved-feature template has a human scope gate and no autonomous planning.
The full main flow allows four planning calls per task. Subscriptions are paced by
their own five-hour and weekly windows, which the Usage view shows with what is left;
an optional queue call cap exists for agents billed per token.

The lead revises a feature's approved tickets when a ticket's agent blocks it or runs
out of repairs. It proposes merging, splitting, revising, cancelling or
guiding tickets and marking what they need (a person, an asset, the web), and you
approve the proposal before it changes anything. A project's **web** setting lets its
ticket agents use the internet, and its **roles** name the agent profile that works as
lead, analyst, implementer and reviewer.

## Daily commands

Use the same `--database .state/features.db` before each subcommand.

| Command | Purpose |
|---|---|
| `status feature-one` | State, stop reason and task version |
| `events feature-one` | Transition history |
| `pause feature-one --version N` | Pause after the active step finishes |
| `stop feature-one --version N` | Terminate the task's owned execution |
| `resume feature-one --version N` | Resume a task |
| `retry feature-one --version N` | Explicitly retry blocked work |
| `answer ID OUTCOME TEXT` | Resolve a human step while the coordinator is stopped |
| `replay feature-one` | Check state against event history |
| `backup backup.db` | Back up the SQLite database |
| `export DIGEST flow.json` | Export a pinned workflow definition |

Read `N` from a fresh status: stale commands are rejected. The `.health.json`
sidecar reports coordinator liveness, not successful task acceptance.

## Architecture and packages

Core has no IO, wall clock, randomness, process or adapter dependencies.
External effects are recorded before execution; only the coordinator applies results.
Plugins are trusted installed code registered explicitly, never imported from
workflow text. Acceptance requires the workflow's gates, matching revisions and
verified evidence; a model's success claim alone is insufficient.

| Package | Responsibility |
|---|---|
| `feature-factory-ai` | Product installation |
| `sdd-core` | Pure contracts, graphs, transitions and SDK |
| `sdd-storage` | SQLite and in-memory transactional storage |
| `sdd-runtime` | Application service, coordinator, executors, supervisor and CLI |
| `sdd-providers` | CLI protocols, discovery and model catalogs |
| `sdd-workflows` | Approved feature, full main flow and interview templates |
| `sdd-factory` | Application layer: drafts, features, specifications, tickets, artifacts |
| `sdd-ui` | Optional local visual workspace over the factory |
| `sdd-usage` | Dated model rate cards and API-equivalent cost of measured tokens |
| `sdd-trackers` | Tracker adapters (Linear): work items in, specification, tickets and states out |

Python module names `sdd_*`, extension groups `sdd.handlers`, `sdd.agents`,
`sdd.executors`, and `.sdd-engine` remain stable technical contracts.
Runtime services do not require `sdd-storage` in custom compositions. In-memory
storage is for tests, not durable queues. See [Replacing modules](docs/REPLACING-MODULES.md)
and [Architecture](docs/ARCHITECTURE.md).

## Verification and limits

A previous Windows engine revision completed approximately **9 hours 47 minutes**
with **3,702 accepted tasks**, two concurrent tasks, 264 forced stops and 120
recoveries. All 7,404 evidence files were checked; no replay mismatches or repeated
attempt generations were found. Model calls: zero. The user stopped the run.
This is not a 24-hour soak or a real-model qualification.

The UI is tested with Playwright in installed Microsoft Edge on Windows. Legacy
queue migration creates paused runs only. OpenCode
read-only execution is not qualified. The remote executor has a contract and a
reference HTTP adapter, not a production server. Linux/macOS CI definitions do not
substitute for real platform runs. See [Verification](docs/REFACTOR-VERIFICATION.md)
and [Roadmap](docs/ROADMAP.md).

## Development

```text
uv sync --locked --all-packages --extra dev
uv run --locked python tools/check_boundaries.py
uv run --locked python -m ruff check .
uv run --locked python -m mypy
uv run --locked python -m pytest
uv run --locked python tools/qualify.py --build --directory reports/local-checks
```

The qualification tool also builds wheels, installs them into a clean environment
and runs the installed demo without models. CI targets Windows/Linux/macOS and
Python 3.12/3.14; local results must name the platform actually tested.
Long-running soak tests are not launched automatically.

Browser tests (PowerShell):

```powershell
$env:FFAI_UI_TESTS = '1'
.venv/Scripts/python.exe -m pytest tests/test_ui_browser.py --browser-channel msedge -q
```

Alternatively install Chromium with `python -m playwright install chromium` and
omit the browser channel. Browser tests use temporary databases and no models.
Update dependencies with `uv lock` and review `uv.lock`.

## License and contributions

[MIT License](LICENSE), Copyright (c) 2026 Erdem Tsynduev.
The repository's [commit skill](.agents/skills/commit/SKILL.md) defines commit checks.
Validate messages with `python tools/check_commit_message.py --file message.txt`.
