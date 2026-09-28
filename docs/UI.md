# Local visual interface

For everyday use, launch **Feature Factory AI** from its desktop shortcut or run
`ffai-app`. No parameters or working directory are needed. It opens the browser,
reuses the same running application on subsequent clicks and stores data under
`%LOCALAPPDATA%/FeatureFactoryAI` on Windows, `~/Library/Application Support/FeatureFactoryAI`
on macOS or `$XDG_DATA_HOME/FeatureFactoryAI` (default `~/.local/share`) on Linux.
Use **Завершить приложение** to stop the server; active executions must finish
first. Closing a browser tab alone leaves the application running.

`ffai --database /absolute/path/to/control/ui.db ui` is the advanced option for a
separate database and port (`--port 8790`, `--no-browser`). Keep the database
outside task workspaces and do not run a second coordinator against it.

## Information architecture

The rail holds the project switcher and six sections: **Board**, **Team**,
**Workflows**, **Agents**, **Usage and limits** and **Log**. The top bar shows the
section, the queue state with its start/pause button, three meters (calls against
the queue budget, measured tokens, API-equivalent cost) and **New task**.

Board and Team belong to the selected project. Without projects (and without
tasks created outside a project) they show a welcome screen: connect agents → add
a project → create a task, with progress read from real state. Tasks created
without a project (CLI runs, the question example) appear under **Без проекта**.
Agents, Workflows, Usage and Log are global.

Routes are hash tokens (`#board`, `#flows`, `#task/<id>`, …). Tabs and task
drawers are browser history entries: Back, the mouse back button and **←** return
to the previous place; the drawer closes with ×, Escape or a click on the backdrop.

## Creating tasks and decomposition

**New task** opens a dialog that asks what kind of work it is:

| Kind | Workflow | What happens |
|---|---|---|
| Large feature | `requirement` | Specification (the agent may ask structured questions) → ticket breakdown → your approval. Approving creates each ticket as a paused child task with its dependencies. |
| Whole task | `main-flow` | Specification, plan, implementation, checks and review in one run; no child tasks. |
| Ready ticket | `ticket` | Implementation, project checks, independent review and fast-forward merge of its lane. |
| Custom | any published version | As drawn in the workflow. |

The server builds the project's version of the chosen template (its checks, lane
settings and language), validates it against the installed handlers and publishes
it on first use; publication is content-addressed, so there is no manual publish
step. A kind whose agent profiles are missing says which profiles to connect.
Title, description, dependencies and options are saved as a draft per project on
every keystroke and restored when the dialog reopens; the draft is cleared only by
creating the task or **Очистить черновик**. **Сразу поставить в очередь** resumes
the task right after creation; otherwise it starts paused.

The requirement's tickets step declares structured `tickets` (id, title, goal,
acceptance, dependencies, owned paths). The approval step shows them; **Одобрить и
создать N тикетов** admits them in dependency order as `<requirement>-<ticket>`
tasks with kind `ticket`, the parent id and a context made of the ticket plus the
approved specification. Admission is idempotent per ticket. **Вернуть на
доработку** requires a comment and routes back to the specification. Without the
`claude` and `codex` profiles, approval is refused before anything is recorded.

## Board, cards and the attention reason

Columns follow one server-side derivation (`sdd_ui.attention`) that names the first
thing blocking each task and the action that resolves it: answer, retry, reconcile,
connect a profile, resume, start the queue — or why it waits (a limit reset, a
resting profile, dependencies). **Queue** holds paused and waiting tasks,
**In progress** running and queued ones, **Needs you** answers and blockers,
**Done** accepted ones. Every card shows kind, title, step and runner, the reason
line and its one-click action; requirements show ticket progress. The nav badge
counts tasks that need you.

Drag cards between Queue and In progress to pause or resume. Nothing can be dragged
into Done: acceptance belongs to gates and review. Search, kind chips and the plan
filter persist per browser. **Дерево** shows plans with requirements and their
tickets nested.

## Task drawer

Top: the attention line with its action, every available command (Продолжить,
Пауза, Остановить, Повторить, Сверить и восстановить), the switch **Агент выбирает
рекомендованные ответы** and the task's path through its workflow.

- **Обсуждение**: the question picker (digits or arrows choose, Enter moves on, the
  recommended option is preselected, **Принять все рекомендации**), the ticket
  approval, results with their notes and token counts, and messages to the next
  step. Answer and message drafts survive closing and reloads.
- **Детали**: facts (flow version, step and runner, calls, tokens, cost, folder),
  parent and child tasks, task memory, the original brief, the lane, the current
  instruction and raw results.
- **Журнал**: the flight log entries of the task, the engine journal, **Скачать
  разбор (JSON)** (state, journal, log entries and log tails of the latest attempt)
  and, when the workflow has a read-only recovery step, **Сверить и восстановить**,
  which hands a short incident summary to that step as guidance and leaves the task
  paused.

## Workflows

The editor draws the workflow as a pipeline: numbered main stages, a recovery lane,
bundled failure wires. **+** on a wire inserts a step, the circle on a step's right
edge starts a link to the next step you pick (with an outcome the step does not
route yet), Delete removes a selected link and **Связь вручную** edits
source/outcome/target directly. Inspector fields apply as soon as they change —
there is no separate "apply" — and renaming a step rewires its incoming links and
recovery references. The draft autosaves in this browser with a visible time;
opening a template or a version over unpublished edits asks first. **Проверить**
validates without model calls; **Опубликовать версию** creates an immutable digest
and existing tasks stay pinned.

## Agents, rotation and limits

**Найти CLI и проверить вход** runs each CLI's `--version` and native status command
(`claude auth status`, `codex login status`, `cursor-agent status`, `opencode auth
list`); none calls a model. **Подключить** writes a runner and a named profile; the
profile name is what steps refer to, so `claude` and `codex` make the bundled
workflows runnable. Changing a profile never rebinds existing tasks silently.

Each profile card has a rotation form: **Если упрётся в лимит — передать работу**
names the fallback, then the rest time, the retry pause and which failures count
(usage limit, rate limit, sign-in, offline). A limited profile rests until its
reset or the rest time; the next attempt uses the fallback and the primary returns
automatically. **Вернуть сейчас** ends a rest early (for example after signing in
again). Without rotation a recognised limit becomes a bounded wait until the reset.

A task never hangs silently: every attempt has a timeout, lost or failed attempts
get up to three automatic retries with backoff, recognised limits wait for their
reset, and after several consecutive infrastructure waits the task blocks with
`Wait retry limit`. With **automatic revival** (Usage and limits, on by default)
the running queue retries such a task once it has rested 30 minutes and its step's
profile or a rotation member is available, at most five times per session; every
revival is logged. Queue budgets still apply.

## Usage and limits

Tokens come from each CLI's own report and are stored with the step result; runs
without a report are marked unreported, never zero. Cost prices those tokens at
public API rates (`sdd-usage`), not a subscription bill. Model calls are reserved
when a step starts, so the queue budget (default 40 calls, 8 of them planning,
cumulative over the database) cannot be exceeded through failures; raise it and
retry blocked tasks explicitly. **Квоты аккаунта Codex** reads account windows
through a temporary Codex app-server without a model turn; it refreshes when this
page opens and the last check is older than ten minutes. Other providers expose no
quota API; their limits appear only in error messages, which the engine classifies.

## Log

The flight log (`<database>.flight.jsonl`, rotated at 4 MiB with three backups)
records operator actions with their outcome and duration, task creation, answers
and admitted tickets, queue starts, pauses, stops and restarts, blocks and waits
with their reasons, and automatic revivals. It never contains tokens or drafts.
Filter by level; entries with a task open it. Together with each task's engine
journal it reconstructs an incident after the fact.

## Projects, themes and languages

**＋** adds a project (name, generated id, existing folder, response language,
check command and lane options) in a dialog with a draft; **⚙** edits the selected
one (its id and folder are fixed). The check command accepts a command line or a
JSON array; its program must be an absolute path. Without a check command generic
check steps are bypassed and review still gates the work.

Theme and language persist per browser; System follows the OS. Interface copy is
keyed (`static/js/i18n/ru.js`, `en.js`, checked by `tests/test_ui_i18n.py`); user
content, agent output and prompts are never translated. Enable browser notifications
explicitly for new questions, blockers and accepted tasks while the page is open.

## Legacy orchestrator queue

`python -m sdd_ui.legacy <portfolios/<id>.sqlite3> --database <ui.db> --apply` creates
a new project named after the legacy workspace and moves every unfinished item into it
as paused runs (the legacy database is read-only and never modified). Plan rows that
were never decomposed become **requirement** runs; unfinished tickets become
**ticket** runs (implement, one gate per legacy check, review, bounded repair) scoped
to their repositories, with dependencies on tickets and on requirement runs. Stop the
old orchestrator yourself; do not run both against the same workspace.

## Front-end structure

Plain ES modules, no build step and no Node.js at runtime:

| Path | Role |
|---|---|
| `js/core/` | `dom` (element builder), `api` (HTTP client, ETag polling), `store` (snapshot, project scope, selectors), `i18n`, `storage` |
| `js/ui/` | `dialog` (modal/drawer, confirm), `toast`, `draft` (form drafts) |
| `js/features/` | One module per screen or concern: `shell`, `board`, `task-drawer`, `answers`, `new-task`, `projects`, `flows`, `agents`, `usage`, `journal`, `team`, `onboarding`, `notifications`, `commands`, `vocabulary` |
| `js/graph/` | `pipeline` (SVG workflow drawing) and `office` (canvas scene) |
| `css/` | `app.css` (tokens and components), `pipeline.css`, `office.css` |

Views register with the shell and re-render from store changes; they never poll.
The server serves any packaged file under `static/` with a known type.

## Browser checks

```powershell
uv sync --locked --all-packages --extra dev
$env:FFAI_UI_TESTS = '1'
.venv/Scripts/python.exe -m pytest tests/test_ui_browser.py -q --browser-channel msedge
```

Or install Chromium with `python -m playwright install chromium` and omit the
channel. Browser tests use temporary databases and make zero model calls; screenshots
go to ignored `reports/ui/`. On September 28 the nine scenarios passed in installed
Edge on Windows (welcome, answer drafts, pipeline editing, new-task drafts, filters
and mobile width, budgets, drag and drop with theme/language and the question
picker, notifications, back navigation). Firefox and WebKit are not qualified.
