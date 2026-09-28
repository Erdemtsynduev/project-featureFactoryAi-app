# Local visual interface

For everyday use, launch **Feature Factory AI** from its desktop shortcut or run
`ffai-app`. No parameters or working directory are needed. It opens the browser,
reuses the same running application on subsequent clicks and stores data under
`%LOCALAPPDATA%/FeatureFactoryAI` on Windows, `~/Library/Application Support/FeatureFactoryAI`
on macOS or `$XDG_DATA_HOME/FeatureFactoryAI` (default `~/.local/share`) on Linux.
Use **Завершить приложение** to stop the server; active executions must finish
first. Closing a browser tab alone leaves the application running.

The `ffai ui` command below is the advanced option for a separate database/port.

Run `ffai --database /absolute/path/to/control/ui.db ui`. The command opens
`http://127.0.0.1:8787` and owns the coordinator lease until stopped with Ctrl+C.
Use `--port 8790` or `--no-browser` when needed. Keep the database outside task
workspaces. No Node.js, external service or model is needed to serve the UI.
Do not run a second coordinator against the same database.

## First run without model calls

1. In **Редактор флоу**, open **Проверка без модели**, validate and publish it.
2. In **Доска заданий**, create a task using that version and a separate existing folder.
3. Open its card, press **Продолжить**, then **Запустить очередь**.
4. The check runs a Python child process; the task reaches **Готово** with zero
   model calls. Open the card to inspect history and evidence.

New tasks and a new queue start paused. Queue pause stops new dispatches while
collecting in-flight results. Task pause lets its current step finish; task stop
terminates its owned processes. Human decisions always require an explicit answer.
Commands carry the observed task version; refresh the card after a conflict.

## Workflows and profiles

The editor exposes nodes, labelled transitions, types, prompts, handlers/profiles,
timeouts, visit limits, required/gate/mutation flags and JSON config. Apply step
edits to the draft before validation/publication. Deleting a node removes its
incoming links; check the resulting graph before publication. Publication validates
the graph and installed handlers, then creates an immutable digest. Existing
tasks stay pinned. Advanced JSON supports all schema fields, including condition
keys and token budgets.

**Сценарии** draws the workflow as a pipeline. The main lane follows each step's
success outcome from the entry (`passed`, `done`, `approved`, `answered`…); its
numbered stages are the real order. Every other step sits in the recovery lane
below the stage that routes to it, and a success chain there (diagnose → repair)
stays side by side. Wires that share an outcome and target (every `failed →
diagnose`) run as one bus with one label; dashed amber wires are failures and
returns. Layout is computed from the graph and never enters the published digest.

Editing: **+** on a straight wire inserts a step between two stages. The circle on
a step's right edge starts a link; the next step you pick becomes its target, with
a proposed outcome that the step does not route yet, so existing routes are never
rewired silently. Select a wire (click, or Enter from the keyboard) and press Delete
to remove it; **Связь вручную** edits source/outcome/target directly. An outcome
has one target; linking an existing outcome replaces that target. Condition steps
have key/value fields in the inspector (see Architecture: conditions).
**Сохранить черновик** stores the draft in this browser; publication remains a
separate validated operation. The task drawer shows the same pipeline with the
current step, completed steps and repeated visits.

**Агенты и лимиты → Найти CLI и проверить вход** runs each CLI's `--version` and its
native status command (`claude auth status`, `codex login status`, `cursor-agent
status`, `opencode auth list`). None of them calls a model; account identifiers are
not shown. Side-by-side versions of one installation (Cursor keeps one folder per
version) resolve to the newest. **Подключить** writes a runner and a named profile
for the discovered executable. The profile name is the handler that steps refer
to, so profiles named `claude` and `codex` make the bundled templates runnable.
Models come from the CLI's catalog where one exists, Codex's configured default,
or documented aliases (`opus`, `sonnet`). Native CLI login stays outside the
application. OpenCode uses `provider/model`; read-only OpenCode remains unqualified.
See [AGENT-PROFILES.md](AGENT-PROFILES.md). Changing a profile used by an existing
task can trigger the configuration-drift guard; restore that configuration or
publish/create a new task. No silent rebinding.

## Budget semantics

**Согласованная задача** offers human scope approval,
implementation, checks, independent review and bounded repair. No model rewrites
plans. The editor initially opens **Main flow · полная команда**, with at most four planning
calls per task. Tag custom planning steps with `{"purpose":"planning"}` in the
step's JSON config string. The engine does not classify arbitrary prompts.

The UI persists default queue ceilings of 40 total calls and 8 planning calls.
These are cumulative ceilings over this database, including failed/reserved calls;
they do not reset daily or predict subscription percentages. After raising a
limit, explicitly retry blocked tasks. Per-workflow limits still apply. Custom
application compositions must supply their own queue policy; the CLI coordinator
does not consume the UI settings file.

## Boundaries

`sdd-ui` is an independent optional application package, loaded through
`ffai.commands`. Core and headless runtime do not import it. The browser sends
versioned commands; it never decides acceptance. Polling does not invoke models.
Loopback HTTP uses exact Host/Origin checks, a mutation token and a restrictive
content policy. This is a local operator interface, not a multi-user remote service.

The HTTP lifecycle is tested with a real server and child process. Playwright
tests exercise the real local HTTP UI in Edge, including questions, graph edits,
draft persistence, remembered filters and mobile layout. Closing the browser does not stop the
server. Automatic restart of the UI server itself is not implemented; the CLI
supervisor is a separate host composition.

## Office, questions and answers

The office on **Задания** is a canvas scene of the project's workflows. People are
roles: one desk per step (planning desks, then delivery, recovery below; every human
step is your desk, every check the tester desk), captioned with the model that runs it.
Tasks are documents: they lie on the desk of their current step. The person there types
at mutating steps, reads at review, watches a spinner at checks, shows "?" at your desk
and "!" when blocked. When a task moves on, the finishing person carries the folder to
the next desk and walks back. Paused and queued tasks wait in the inbox tray, accepted
ones on the shelf; idle people fetch coffee. Hover names the documents; clicking a stack
opens the task, clicking a desk opens that step. Reduced motion stops walking.

Questions open as a picker: digits or arrows choose an option, Enter moves to the next
question, the recommended option is marked and preselected. **Принять все рекомендации**
answers at once; a comment is optional. The drawer switch **Агент выбирает
рекомендованные ответы** makes the coordinator accept recommendations while the queue
runs; the answer is recorded like a human one with an `auto` flag.

## Workshop and questions

The board groups tasks by their recorded state. Human steps appear in **Нужны вы**
and in the question inbox even when the engine's status is `running`. Pixel workers
reflect this database's active steps. Select a role
to open its workflow step or active task. Questions show the human-step prompt and
recent agent results. Draft answers survive polling and card reloads in the current
page; submitting carries the observed version and is never automatic.

## Legacy orchestrator queue

### Move the queue onto this engine

`python -m sdd_ui.legacy <portfolios/<id>.sqlite3> --database <ui.db> --apply` creates
a new project named after the legacy workspace and moves every unfinished item into it
as paused runs (the legacy database is read-only and never modified). Plan rows that
were never decomposed become **requirement** runs (`requirement` workflow: specification,
structured questions, ticket breakdown, your approval). Unfinished tickets become
**ticket** runs (implement, one gate per legacy check, review, bounded repair) scoped to
their repositories, with dependencies on tickets and on requirement runs. Plans are
stored as board groupings with their historical progress. Requirement runs claim only
the plans folder, so planning never blocks delivery. Re-running keeps identical runs.

Checks are resolved once: `python` is the project interpreter on PATH (never this
engine's venv; `--python` overrides), `git` its absolute path, `{base:<repo>}` the
repository HEAD at import (Git's empty tree for a repository the ticket creates).
Tickets are isolated in worktree lanes and merge conflicts go to an agent by default
(`--shared-workspace` and `--manual-conflicts` turn that off). Stop the old
orchestrator yourself; do not run both against the same workspace.

## Board, plans and kinds

Cards show their kind: **Требование** (a plan row to specify; tinted document card),
**Тикет** (a unit to build, with its checks and parent requirement) and plain
**Задание**. Chips filter by kind; the plan filter works on the live board. **Планы**
lists plans with progress (historical acceptance plus this engine's) and expands into
requirements with their tickets. Every card names the step and who runs it, e.g.
`claude · opus`, taken from the connected profile.

Tabs and task cards are browser history entries: Back (browser, mouse or the "←" in the
top bar) returns to the previous tab or card. **Первые шаги** is a checklist whose
progress is read from real state (agents, project, workflow, task, queue, answer);
**Короткий тур** highlights the main areas. **Как пользоваться** reopens both.

## Projects, themes and languages

Use **Add project** to register another existing folder. **Edit project** updates
the selected label, response language and check command. Its workspace is immutable;
register another project to use another folder. The selector filters live cards
and workers. Project checks are copied into newly loaded templates. Publishing a
command-check workflow requires an explicit absolute executable.

Theme and language persist in browser local storage. Theme is applied before styles;
only System follows OS changes. Russian/English is the default response language
for new tasks and is recorded in their context. Existing content is not translated.
README and code comments are English.

Drag live cards between Ready and In progress to pause/resume. Starting the queue
is separate. Human questions require an answer; cards cannot be dragged into success.
The drawer has Discussion, Details and Events tabs, and closes with Escape.
Keyboard buttons offer the same task commands without dragging.

The question-example button creates a human-only task in a separate sample folder,
opens its question and makes zero model calls. Answers resolve the waiting step.
Operator messages are saved idempotently, recorded in history and included in the
next invocation. Running processes keep their original packets. Drafts survive
polling within the page.

Enable browser notifications explicitly for new questions, blockers and completion
while the page is open. Browser/OS permission is required; this is not background
push. The in-page inbox always remains available. Idle workers have coffee/phone
and screen animation; mail moves on an observed step change. Reduced-motion
preferences disable animation.

## Recovery and usage

Main flow includes a declared read-only reconcile step. Confirmed crash recovery
goes there before checks and review. The drawer recovery action is available for
inactive blocked/waiting tasks with a recovery path. It records the observed
workspace revision, clears obsolete gates and leaves the task paused. Required
gates, budgets and visit limits still apply after explicit resume.

Detailed prompts live in sdd_workflows/prompts.py. Old drafts and pinned definitions
are not overwritten; reload the Main flow template to adopt the new prompts.

Settings show queue call/planning meters, daily step dispatches and measured tokens.
Unknown usage remains labelled. CLI discovery runs bounded version and login-status
probes; a successful status is not a successful model call. Refresh Codex quotas starts a temporary native Codex app-server,
initializes it, reads account/rateLimits/read and terminates that helper. It creates
no thread or model turn. One unambiguous installation is required; otherwise set
its exact path in profiles. The snapshot shows account-wide used percentages,
window lengths, resets and sample time. Other providers and unsupported endpoints
have no invented quota/auth values. See [Legacy comparison](LEGACY-PARITY.md).

## Browser checks

```powershell
uv sync --locked --all-packages --extra dev
.venv/Scripts/python.exe -m playwright install chromium
$env:FFAI_UI_TESTS = '1'
.venv/Scripts/python.exe -m pytest tests/test_ui_browser.py -q
```

On Windows an installed Edge can be used without downloading Chromium:
append `--browser-channel msedge` to pytest. The browser tests use temporary
databases and make zero model calls. Without `FFAI_UI_TESTS=1` they are skipped;
the rest of `pytest` needs no installed browser. Screenshots go to ignored
`reports/ui/`. The local September 28 run used Edge because the Playwright Chromium
download timed out; Firefox and WebKit have not been qualified.

Design references: [React Flow custom nodes](https://reactflow.dev/learn/customization/custom-nodes),
[taste-skill redesign guidance](https://github.com/Leonxlnx/taste-skill/blob/main/skills/redesign-skill/SKILL.md),
and [pixel-agents](https://github.com/pixel-agents-hq/pixel-agents).
The current editor uses local SVG and CSS, preserving the Python-only runtime.

September 28 verification: 179 tests passed on Windows, including six Playwright
scenarios in Edge. Ruff, strict mypy and package-boundary checks passed. Wheels
were installed in a clean environment and served all UI assets successfully.
The native Codex quota endpoint was read successfully without a model call.
Recovery and repair tests use controlled results; they do not qualify live models
or claim production parity with the old autonomous portfolio scheduler.

## Lanes, rotation, cost and remembered choices

**Lanes.** Projects default to "each ticket in its own worktree" and "an agent resolves
merge conflicts" (project form). A ticket's first attempt creates
`<workspace>/.sdd-lanes/<task>/`: its repositories as worktrees on `ffai/<task>`, the
rest of the workspace linked. After review the ticket merges itself: fast-forward only;
a moved base is rebased and every check and the review run again; a conflict goes to
the resolve agent or to you (**Конфликт слияния**). After acceptance the worktrees,
merged branches and links are removed and evidence moves back to `.sdd-engine`. The
task drawer shows the lane, branch and bases. Linked folders are other checkouts:
agents are told not to edit them, but the operating system does not prevent it.

**Rotation.** Each profile card has "При сбое переключаться на": pick a fallback
profile, the failures that trigger it (usage limit, rate limit, sign-in, offline), the
rest time and the retry pause. A limited profile rests until its reset (or the rest
time); the next attempt uses the fallback, and the primary returns automatically.
Without rotation a recognised limit becomes a bounded wait until the reset instead of a
blocker. Resting profiles are shown with their end time.

**Cost.** `sdd-usage` prices measured tokens at public API rates (dated cards, cache
read/write, long context). The top bar, agent cards and task cards show the equivalent;
subscription plans are billed separately and unknown models stay unpriced.

**Remembered choices.** Search, plan and kind filters, board or plans view, open plans,
the drawer tab, answer and message drafts and the workflow draft survive reloads (per
browser). The last CLI discovery is stored in the database and survives restarts.
