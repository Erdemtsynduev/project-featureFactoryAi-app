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

The rail holds the project switcher and six sections: **Overview** (the default:
key numbers, Needs you, Running now, the team office, recent events, agent health
and plan progress), **Board**, **Workflows**, **Agents**, **Usage and limits** and
**Log**. Design rules and the latest review are in [DESIGN.md](DESIGN.md). The top bar shows the
section, the queue state with its start/pause button, three meters (calls against
the queue budget, measured tokens, API-equivalent cost) and **New task**.

Overview and Board belong to the selected project. Without projects (and without
tasks created outside a project) they show a welcome screen: connect agents → add
a project → create a task, with progress read from real state. Tasks created
without a project (CLI runs, the question example) appear under **Без проекта**.
Agents, Workflows, Usage and Log are global.

Routes are hash tokens (`#board`, `#flows`, `#task/<id>`, …). Tabs and task
drawers are browser history entries: Back, the mouse back button and **←** return
to the previous place; the drawer closes with ×, Escape or a click on the backdrop.

## Starting work, progress and notifications

A queue only takes resumed tasks. **Запустить** on a card or in the drawer resumes
the task and, when the queue is paused, starts it too; if other resumed tasks would
start with it, a dialog asks first (**Только разрешить задачу** resumes this one
alone). **Запустить задачи…** on the board, **Запустить план** on a plan row and
**Запустить вместе с зависимостями** in the drawer open one dialog for many tasks:
the board filter, one plan or one task. It offers tasks ready to start (all
dependencies accepted) or every paused task (the rest wait for their dependencies),
lists the paused prerequisites outside the selection (other plans, no plan) with a
switch to start them too, names blocked prerequisites it cannot start, and warns
when the tasks outnumber the calls left in the queue budget. The server applies the
same selection (`resume-many` with `kind`, `plan`, `ids`, `with_dependencies`);
prerequisites are followed transitively within the project. **Пауза всем** and a
plan's **Пауза** ask before pausing. Starting the queue while no task of the
project is resumed opens the dialog instead of silently running an idle queue.
Dragging a card to In progress only resumes it and says what happens next (queued,
waiting for a dependency, queue paused).

A running step shows a spinner, its elapsed time and the share of its timeout on
the card. In the drawer, **Сейчас** adds the timeout moment, how long ago the agent
last wrote output (with a warning after five quiet minutes) and the tail of its
output, refreshed every two seconds (`/api/live`); streamed Codex events are
summarised one per line.

The bell in the top bar opens the notification center: tasks that need you in any
project, then events from the flight log since you last read it (agent questions,
blocks, limit waits, acceptances, created tickets, revivals, queue stops, bulk
resumes), and a switch for system notifications while the page is open.

The rail shows the engine version; **About the engine** lists every library version
and flags a mixed installation.

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
step. A kind whose agent profiles are missing says which profiles to connect. Under the
kinds, **Что будет дальше** explains the project scope (folder, language, checks)
and the numbered steps of the chosen kind, including decomposition into tickets.
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

## Work is a tree

Any run whose workflow declares `tickets` and gets them approved becomes a parent,
so a ticket whose workflow has its own breakdown step holds sub-tickets, to any
depth. A parent's own run finishing (the feature's `accepted` step after approval)
means its **planning** is done, not the work. From then on the server derives the
parent's reason from its children (`sdd_ui.attention.rollup`), with `progress`
(children done / all):

| Reason | When | Lane |
|---|---|---|
| `delivery_paused` | no child done, all children idle | Queue, **Запустить тикеты** |
| `partial` | some done, the rest idle | Queue, **Доделать** / **Закрыть частично** |
| `delivery_waiting` | a child waits for a limit or time | Queue |
| `delivering` | a child runs | In progress |
| `children_need` | a child needs an answer or is blocked | Needs you |
| `delivered` | every child is done (recursively) | Done |
| `closed` | closed early by the operator | Done |

A workflow without a breakdown (a question turned into a task, a whole task)
is a leaf: it is done when its own run is accepted. **Доделать** resumes every
unfinished run below the parent with its prerequisites (the bulk dialog).
**Закрыть частично** (`close`) records the parent as closed and detaches each
child that is not finished (its run or anything below it): the child becomes
top-level work with `origin` naming the parent. Nothing stops, runs or is removed;
the records are application metadata, so the engine's history is unchanged.
Dependencies between runs still mean "run accepted": work that depends on a
feature waits for its planning, not its delivery.

## Features, plans and where artifacts live

The factory's input is a **feature**. Every feature takes the same path, as in
mobile-sdd-factory (a Jira task → proposal, requirements, acceptance, constraints →
decomposition → subtasks) and AI Hero (grill me → PRD → PRD to issues → TDD per issue):

1. **Specification** — a PRD: problem, solution, numbered user stories,
   implementation and testing decisions, acceptance criteria `AC-n` traced to the
   source, out of scope. When a decision only you can make is missing, the agent
   asks structured questions first.
2. **Tickets** — tracer-bullet vertical slices with goal, acceptance, `depends_on`
   (only real blockers, so independent tickets run in parallel) and the repository
   folders each owns; `HITL:` marks a ticket that needs a person inside it.
3. **Your approval** of the specification and tickets as a whole.
4. **Ticket runs** — implementation on its own lane, project checks, independent
   review, merge. A ticket claims only the repositories it owns.

A feature comes from **New task → Большая фича**, or from a **plan**: a plan file
`NNN_*.md` in the project's **plans folder** (a project setting; none by default)
becomes one feature `feature_<NNN>` whose scope is the plan's
open and partial rows (closed and rejected rows are context). Rows the old queue
already decomposed into tickets are not in scope again; those tickets are listed in
the feature's brief. **Обновить планы из файлов** (`plans-sync`) creates a feature per
plan that has uncovered open rows, and a follow-up feature `feature_<NNN>_<n>` for rows
added later (research adds rows). **Пересоздать доску из планов** (`plans-rebuild`)
backs the database up and plans again everything that never started, under the
current rules (one repository per ticket, chains across repositories): never-started
features and tickets leave the board, carrying what they recorded (drafts, requirement,
acceptance) into the new feature; a feature whose tickets left is closed, its started
tickets keep running as top-level work and its rows are planned again. With `plan` it
touches one plan. Nothing edits plan files or starts work. A feature's brief lists the
plan's queued tickets with their state and its delivered tickets, so neither is
planned twice. A started per-row requirement of the earlier import whose row a feature
now plans is marked superseded by that feature (**Заменено**, in the done column) and
paused; its recorded work is already in the feature's brief. Started tickets that cannot finish
(for example ones spanning repositories) are handed to the feature that plans them
again with the `supersede` action: they pause, show as **Заменено**, and keep their
lane and `ffai/<run>` branch so the new tickets can reuse the work.

The specification and the tickets belong to the factory, not to the project: they are
recorded results in its database, shown in the feature's drawer under **Документы
фичи** (with **Скачать spec.md**) and written on approval to
`<data folder>/artifacts/<project>/<feature>/spec.md` and `tickets.md`. The project
repository receives only the tickets' code. Read-only runs (specifications) do not
hold their folder between attempts, so many features advance side by side.

## Board, cards and the attention reason

The board has three views. **Дерево** (the default) shows features with their
tickets and sub-tickets, like sub-issues: each row has the reason line, the
children's progress for a parent and the one-click action; needs-you and running
work comes first. Chips narrow the tree to one lane and **Скрыть готовые** hides
done work; a match's ancestors stay, dimmed, for context. Rows with work left start
unfolded; folds are remembered. **Канбан** shows the columns below with only work
that moves by itself: an approved parent is represented by its tickets there and in
**По планам**.

Columns follow one server-side derivation (`sdd_ui.attention`) that names the first
thing blocking each task and the action that resolves it: answer, retry, reconcile,
connect a profile, resume, start the queue — or why it waits (a limit reset, a
resting profile, dependencies). **Queue** holds paused and waiting tasks,
**In progress** running and queued ones, **Needs you** answers and blockers,
**Done** accepted ones. Every card shows kind, plan, title, step and runner, the
reason line and its one-click action; requirements show ticket progress, tickets
their parent. A task waiting for dependencies names them by title. The nav badge
counts tasks that need you; tasks that need you in another project (or without a
project) are listed above the board and in Overview with a link there.

Columns show 40 cards and **Показать ещё** pages further. Drag cards between Queue
and In progress to pause or resume. Nothing can be dragged into Done: acceptance
belongs to gates and review. The view, search, lane chips and the plan filter
persist per browser.

**По планам** shows one collapsible row per plan (Epic-style swimlanes): counts per
column, progress, how many tasks outside the plan it waits for, **Запустить план**
and **Пауза**; an open row holds the same four columns for that plan only. Rows
render their cards only when open, so a hundred plans stay cheap; the open rows are
remembered. Overview's plan list opens the plan's row.

## Task drawer

Top: the attention line with its action; for a task waiting on others, **Ждёт
приёмки N задач** with each prerequisite, its state and a link, plus **Запустить
вместе с зависимостями** when any of them is paused; the other available commands
(Запустить, Пауза, Остановить, Повторить, Сверить и восстановить — the one already
in the attention line is not repeated), the switch **Агент выбирает рекомендованные
ответы** and, folded, the task's path through its workflow.

- **Обсуждение**: the question picker (digits or arrows choose, Enter moves on, the
  recommended option is preselected, **Принять все рекомендации**), the ticket
  approval, results with their notes and token counts, and messages to the next
  step. Answer and message drafts survive closing and reloads.
- **Детали**: facts (flow version, step and runner, calls, tokens, cost, folder),
  parent and child tasks, **Ждёт задачи** and **От неё зависят**, task memory, the
  original brief, the lane, the current instruction and raw results.
- **Журнал**: the flight log entries of the task, the engine journal, **Скачать
  разбор (JSON)** (state, journal, log entries and log tails of the latest attempt)
  and, when the workflow has a read-only recovery step, **Сверить и восстановить**,
  which hands a short incident summary to that step as guidance and leaves the task
  paused.

## Workflows

The editor opens in **Просмотр**: fields are disabled, the graph has no edit
handles and selecting a step shows what it does. **Редактирование** enables editing
(the choice is remembered; unpublished edits open in edit mode). The editor draws
the workflow as a pipeline: numbered main stages, a recovery lane, bundled failure
wires. **+** on a wire inserts a step, the circle on a step's right edge starts a
link to the next step you pick (with an outcome the step does not
route yet), Delete removes a selected link and **Связь вручную** edits
source/outcome/target directly. Inspector fields apply as soon as they change —
there is no separate "apply" — and renaming a step rewires its incoming links and
recovery references. The draft autosaves in this browser with a visible time;
opening a template or a version over unpublished edits asks first. **Проверить**
validates without model calls; **Опубликовать версию** creates an immutable digest
and existing tasks stay pinned.

A task's drawer shows **Флоу этой задачи** while it is unfinished and idle: **Пропустить**
a step (a required step, marked `*`, asks first), choose another agent profile for an agent
step, or **Обновить до текущего шаблона**. The plans header's **Обновить флоу незавершённой
работы** reports how many unfinished tasks run an older template version and moves the idle
ones. Each change publishes a new version and migrates the task; progress stays where steps
are unchanged.

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
public API rates (`sdd-usage`), not a subscription bill.

**Подписки: сколько осталось** shows, for every subscription the agent profiles use
(Claude and Codex), each window's remaining share and reset time, and which agents
rest until a spent window resets. The server reads them on its own schedule (every
15 minutes, every 5 near a limit, at once after a refusal) without a model turn;
**Refresh** reads now. A resting agent's steps wait unstarted, so no attempt is
spent on a refusal, and they start by themselves at the reset.

The queue call cap is optional and empty by default: it is for agents billed per
token. Attempts that never reached a model (a host that died before starting, an
immediate provider refusal) are not counted. Settings saved with the earlier
defaults (40 calls, 8 of them planning) are read as no cap.

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

**Приложение…** at the bottom of the rail restarts the server (code updates apply, the
queue state is kept, the page reloads itself) or quits it; both wait for active agent
runs. The application checks every 30 seconds whether the engine code on disk is newer
than the running code and then offers **Перезапустить**.

A project may name **checks per repository** (`folder: command`, one per line): a
ticket runs the checks of the repositories it owns, otherwise the project's checks.

Theme and language persist per browser; System follows the OS. Interface copy is
keyed (`static/js/i18n/ru.js`, `en.js`, checked by `tests/test_ui_i18n.py`); user
content, agent output and prompts are never translated. Enable browser notifications
explicitly for new questions, blockers and accepted tasks while the page is open.

## Legacy orchestrator queue

`python -m sdd_factory.legacy <portfolios/<id>.sqlite3> --database <ui.db> --apply` creates
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
| `js/features/` | One module per screen or concern: `shell`, `board`, `task-drawer`, `answers`, `new-task`, `projects`, `flows`, `agents`, `usage`, `journal`, `team`, `onboarding`, `notifications`, `commands`, `vocabulary`, `bulk`, `dashboard`, `work` (parents: finish, close) |
| `js/board/` | The board's parts: `state` (filters, open plans, folds, paging), `tree` (the work tree), `cards` (cards and columns), `plans` (plan rows and actions) |
| `js/drawer/` | The task drawer's tabs: `discussion`, `details` (dependencies, documents), `log`, `live`, shared `parts` |
| `js/flows/` | The workflow editor's `state` (draft, mode, elements) and `inspector` |
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
go to ignored `reports/ui/`. On September 29 the twelfth scenario (the work tree:
nesting, partial progress, folds, lane focus, closing partly done) was added; on
September 28 the eleven scenarios passed in installed
Edge on Windows (welcome, answer drafts, read-only view and pipeline editing,
new-task drafts, filters and mobile width, budgets, drag and drop with
theme/language and the question picker, notifications, bulk start, starting a plan
with an outside dependency and column paging, back navigation). Firefox and WebKit
are not qualified.
