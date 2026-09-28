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
- **Tone vocabulary.** `done` green, `working` blue, `waiting` amber, `attention`
  accent, `blocked` red, `idle` grey — the same meaning on cards, rows, pills,
  dots and notices.

## Information architecture

| Section | Purpose |
|---|---|
| Overview (default) | Key numbers, Needs you, Running now, the team office, recent events, agent health, plan progress |
| Board | Every task by what it needs; filters; bulk resume/pause for the filtered tasks |
| Workflows | Custom workflow authoring |
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

Open items:

- Card titles repeat the plan and ticket ids already shown in chips and the parent
  line; imported titles should drop the prefix once the ticket id is a field.
- Requirements imported from the legacy queue dominate counts (1491 of 1545);
  Overview should show requirements and tickets separately.
- The Workflows editor is dense for occasional use; a read-only view with an
  explicit "Edit" mode would make accidental edits impossible.
- The pixel office does not scale beyond about fifteen desks; a compact list mode
  of roles with their current task is needed for large flows.
- Dark theme contrast of muted text on raised surfaces should be measured, not
  judged by eye.
