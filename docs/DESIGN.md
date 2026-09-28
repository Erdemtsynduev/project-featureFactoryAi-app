# Operator console design

Rules for every screen of the local UI, then the review they came from. A screen
that breaks a rule is a defect, not a style choice.

## Principles

- **Every item answers three questions**: what is it, what can I do to it, where
  does it take me. A card shows its title, one reason line and one action.
- **One reason, one action.** Tasks show the server's attention reason
  (`sdd_ui.attention`) and the single action that resolves it. Views never derive
  task state themselves.
- **One home per object.** A task is managed in its drawer; the board, overview,
  notifications and log link there instead of growing their own controls.
- **Say what happens before it happens.** Anything that starts work, changes many
  tasks or loses input goes through a dialog that names the effect and its scope
  (resume, pause all, replace a draft, quit).
- **Nothing silent.** A running step shows elapsed time and output freshness; an
  idle queue says why it is idle; a drag says what the task will do next.
- **Drafts are free.** Typed text survives closing, reloads and polling.
- **Calm density.** Chrome is one top bar and the rail. Lists clamp prose to two or
  three lines; artifacts (a question being answered, an output tail, a brief) are
  shown whole in a bounded scroll region. Primary blue is for the one decision on a
  surface; repeated actions in lists use the soft style.
- **Stable layout.** Numbers are tabular, labels that switch reserve the longest
  width, long names truncate with an ellipsis, pages never scroll sideways.
- **Work has a shape.** Plan → requirement → ticket is the hierarchy (Epic → Story
  → Sub-task). A plan is something you start and pause as a unit; a task that waits
  shows what it waits for by name, with links, and one way to start it all.
- **One word per effect.** "Запустить" means "let it work, and start the queue if
  it is paused"; the queue itself is the only other switch. A button never says
  less than it does.
- **Nothing hidden by volume.** Long lists page ("Show more"), never end in a
  dead "refine the filter"; what needs you elsewhere is linked from here.
- **Tone vocabulary.** `done` green, `working` blue, `waiting` amber, `attention`
  accent, `blocked` red, `idle` grey — the same meaning on cards, rows, pills,
  dots and notices.

## Information architecture

| Section | Purpose |
|---|---|
| Overview (default) | Key numbers, Needs you, Running now, the team office, recent events, agent health, plan progress |
| Board | Every task by what it needs, by status or by plan; start/pause a plan, the filtered tasks or a task with its dependencies |
| Workflows | Read-only view of every workflow; explicit Edit for custom authoring |
| Agents | CLIs, profiles, rotation |
| Usage and limits | Budgets, spend, quotas, revival |
| Log | The flight log |
| Bell (top bar) | Needs you in every project, events since last read |

The team office moved from its own section into Overview: on its own it carried
no numbers and left most of the page empty; next to the live lists it shows who
works on what.

## Review, September 28 (real database: 1 project, 1545 imported tasks)

Fixed in this round:

1. The team had its own section with a decorative scene and an empty page — now a
   collapsible panel of Overview beside live numbers and lists.
2. The board's Queue listed 1545 cards, each with the same primary "Resume" — the
   queue now groups "Ready to start" before "Waiting or paused", and resume uses
   the soft style; bulk actions follow the board filter.
3. Below 1280 px the lanes stacked, pushing Needs you below hundreds of cards —
   lanes now keep their order and scroll sideways; each lane scrolls on its own.
4. Agents showed a full connection form for every CLI, even connected ones — a
   connected CLI shows its facts and hides the form under "Change this CLI's
   profile".
5. Buttons with truncated content were centered, so long names lost both ends —
   link buttons align left and truncate at the end.
6. The office canvas could overflow a narrow panel — it scrolls inside its panel.

## Review 2, September 28 (same database, 110 plans)

The operator could not start a plan: tasks waited on other plans, the board showed
40 of 1503 queued cards with no way to see more, and the filtered bulk resume
resumed the whole project.

Fixed in this round:

1. **Defect:** "Resume tasks…" showed counts for the board filter but posted only
   the project, so a plan filter resumed every task. The dialog now sends the same
   selection the server applies (`kind`, `plan`, `ids`), covered by a test.
2. There was no plan-level action. **By plan** shows one row per plan (counts per
   column, progress, outside waits) with **Start plan** and **Pause**; an open row
   holds that plan's columns.
3. Dependencies were invisible: the reason line listed raw ids and the drawer had
   no dependency section. Cards name what a task waits for; the drawer shows the
   waits with states and links under the banner, **Waits for** / **Needed by** in
   Details, and **Start with dependencies**.
4. Starting a plan or a task offers the paused prerequisites outside the selection
   (other plans, no plan) and names blocked ones; the server resumes them
   transitively within the project.
5. Columns stopped at 40 cards with "refine the filter" — they now page with **Show
   more**.
6. Three near-synonyms (Resume, Resume tasks, Start queue) — card and drawer
   **Start** resumes and starts a paused queue, asking only when other tasks would
   start with it.
7. The dialogs never compared the work with the call budget; they now warn when the
   tasks outnumber the calls left, and Usage says so for the project.
8. A question waiting in another project (or with no project) was invisible from
   the board and Overview ("Needs you · 0"); both now link to it.
9. The drawer repeated its primary action in the command row and opened with the
   workflow diagram taking half the screen; the action appears once and the path is
   folded with the current step in its summary.
10. Overview listed plans by bare number and counted 1491 requirements and 54
    tickets as one "accepted" figure; plans show titles, open their row, and the
    tile splits requirements and tickets. The office is folded while nobody works.
11. The Workflows editor opened editable; it now opens in **View** with an explicit
    **Edit**.
12. Imported tickets link a parent requirement that was never imported; the card
    shows its short id and the drawer no longer offers a broken link.

## Review 3, September 28 (plan 110, "Run is not dispatchable")

The operator still could not run a plan, and a task showed "Blocked: Run is not
dispatchable".

Found and fixed:

1. **Core defect — false block.** The coordinator read a task, the operator paused
   it, then dispatch refused the paused task and the coordinator blocked it. The
   engine now re-checks the operator state (`machine.dispatchable`) and skips such a
   task as a conflict; tasks blocked this way by earlier builds are released once on
   start and logged.
2. **Core defect — one requirement at a time.** Every requirement claims the plan
   folder, and any started unaccepted run held its claim until acceptance. A single
   requirement waiting for approval (`002_MV6-16`) kept all 1491 others from ever
   starting. Now only a run that dispatched a mutating step holds its paths between
   attempts (`machine.holds_claim`); read-only planning runs share the folder.
3. **What a plan is** was nowhere in the product. Plans are now read from their
   files (`sdd_workflows.plans`), the By plan view explains the pipeline, new rows
   from research become requirements on **Refresh plans from files**, and progress
   follows the file.
4. The legacy import created 129 requirements from rows the file rejects (`[-]`).
   Starting a plan skips rows closed in the file, and both the row and the card say
   so.
5. Starting plan 110 needs at least 84 planning calls against a budget of 8 for the
   whole database; the dialog now shows the estimate and raises both budgets in
   one explicit click.

## Review 4, September 28 (the input artifact)

The plans were written for a person and the legacy driver, not as factory input: of
1404 open rows, 68 had acceptance criteria and 241 an owning repository. A first
attempt gave plans their own strict format inside the project (`.ffai/plans`) and ran
"ready" rows directly as tickets. It was withdrawn: it skipped the specification and
put factory artifacts into the product repository.

The model now matches mobile-sdd-factory and AI Hero:

1. The input is a **feature**; a plan is one feature whose scope is its open rows
   (110 plans → 102 features; 8 plans are fully covered by queued legacy tickets).
2. Every feature gets a **specification (PRD)** and a **breakdown into vertical
   slices**, approved once; per-row specifications (two planning calls and one
   approval per row, 1404 rows) are gone.
3. **Artifacts live in the factory** (database and `artifacts/<project>/<feature>/`),
   the project only receives code; the drawer shows the specification and tickets.
4. Tickets claim only their repositories, so independent tickets run in parallel.
5. **Rebuild the board from plans** replaced 1486 never-started per-row requirements
   by 102 features on a copy of the real database, keeping started work, the 54
   legacy tickets and the 61 recorded specification drafts.

## Review 5, September 28 (architecture)

Done after the architecture review:

1. **The product has its own layer.** `sdd-factory` holds the vocabulary (typed
   `TaskRecord`: feature, ticket, task), the catalog, the use cases, plan sources and
   the legacy import; the UI package is the console over it. Records can be
   corrected (`rename`); "requirement" is read as "feature".
2. **Artifacts are first class.** Steps declare what they produce; the specification
   and the breakdown are stored per feature and exported by the factory.
3. **Typed step options** replace eighteen ad hoc JSON reads; publication refuses
   unknown options.
4. **One source of truth for queue settings** (the database). Profiles stay a file
   because the CLI shares it (`--config`).
5. **Read model:** batched reads, server-derived lane and call estimates, no prompts
   in the snapshot: 0.2 s → 0.065 s and 1.7 MB → 1.3 MB on 1546 runs.
6. **Coordinator readiness:** waiting runs are not candidates, and slots and claims
   are checked before lanes and Git.
7. **Feature sources:** `FeatureSource` with the Markdown plan adapter.
8. **Front end:** the drawer, the board and the workflow editor are split into
   modules (`js/drawer`, `js/board`, `js/flows`).
9. **Checks per repository** for tickets; `with_checks` no longer overwrites a
   configured check.
10. **Restart from the console**, keeping the queue running when it was running.

Open items:

- Accepted work is not written back to the plan files; a feature's acceptance
  could close its rows through the project's own tool as a recorded operation step.
- The snapshot is still the whole board every poll (ETag avoids re-sending an
  unchanged one); a change feed would scale past a few thousand runs.
- Agent profiles live in a file shared with the CLI; moving them into the database
  needs the CLI to read the catalog.
- Card titles repeat the ticket id already shown in the title prefix and the parent
  line; imported titles should drop the prefix once the ticket id is a field.
- A plan's order and its cross-plan dependencies are not visualised as a graph;
  a dependency view (critical path, what unblocks most) would help sequencing.
- The pixel office does not scale beyond about fifteen desks; a compact list mode
  of roles with their current task is needed for large flows.
- Dark theme contrast of muted text on raised surfaces should be measured, not
  judged by eye.
