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
keys and token budgets. Drag nodes, pan the background, zoom or use **Уместить**.
Connect an output circle to an input circle, or use the source/outcome/target form.
An outcome has one target; connecting the same outcome replaces that target.
Edges can be selected with the keyboard (Enter), then removed with **Удалить связь**.
**Сохранить черновик** stores the applied workflow and node positions in this
browser's local storage; publication remains a separate, validated operation.

Use **Команда и настройки** to define the executable, adapter, exact model ID, rights
and timeout. Add the profile to the draft, then validate/save it. Native CLI login
stays outside the application. OpenCode uses `provider/model`; read-only OpenCode
remains unqualified. See [AGENT-PROFILES.md](AGENT-PROFILES.md).
Changing a profile used by an existing task can trigger the configuration-drift
guard; restore that configuration or publish/create a new task. No silent rebinding.

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
draft persistence, snapshot filtering and mobile layout. Closing the browser does not stop the
server. Automatic restart of the UI server itself is not implemented; the CLI
supervisor is a separate host composition.

## Workshop and questions

The board groups tasks by their recorded state. Human steps appear in **Нужны вы**
and in the question inbox even when the engine's status is `running`. Pixel workers
reflect this database's active steps; snapshots never animate them. Select a role
to open its workflow step or active task. Questions show the human-step prompt and
recent agent results. Draft answers survive polling and card reloads in the current
page; submitting carries the observed version and is never automatic.

## View plans from the legacy orchestrator

Import a read-only snapshot into a separate preview workspace:

```powershell
.venv/Scripts/python.exe -m sdd_ui.preview C:/path/to/portfolio.sqlite3 --database .state/workshop/ui.db
.venv/Scripts/python.exe -m sdd_runtime.cli --database .state/workshop/ui.db ui --port 8791
```

The importer opens SQLite with `mode=ro`, copies ticket acceptance/dependencies
and remaining source requirements into `ui.preview.json`, and creates **no engine
runs**. It never loads the old application's code, copies runnable commands or
starts queues. Existing snapshots are not overwritten: choose a new database name
to capture another revision. The server loads the sidecar at startup.

**Планы оркестратора** shows source states, capture time, and original file paths.
Search and filter by plan; each lane initially shows up to 40 matches. The snapshot
opens automatically when the live queue is empty. Source acceptance is historical
data, not acceptance by this engine. Opening a snapshot card offers no run controls.

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
Unknown usage remains labelled. CLI discovery runs bounded version probes, not
authentication. Refresh Codex quotas starts a temporary native Codex app-server,
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
