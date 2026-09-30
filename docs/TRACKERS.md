# Trackers: work in, progress out

A project's drafts come from its **work sources**: the numbered Markdown files in its
drafts folder, and the tracker it names (Linear today). Both may be set at once; a
tracker is one more source. A tracker also receives the factory's progress back:

| Direction | What | How |
|---|---|---|
| In | Work items → drafts; their open rows → the draft's scope | `WorkSource.items(workspace)` during **drafts import** |
| Out | The features cut from an item | issues in the project, or sub-issues, with "blocks" relations along dependencies |
| Out | Approved specification | a comment (issue) or a project update (project) |
| Out | Approved tickets | sub-issues of the feature's issue, with "blocks" relations along dependencies |
| Out | States: queued, working, needs a person, blocked, done | issue workflow state; project update health |

## Contracts (`sdd_core.tracking`)

- `WorkItem(key, title, body, rows, path="", link="", url="")` and `WorkRow(id, mark,
  text, detail, link)`: neutral types every source returns. `key` names the draft
  (`draft_<key>`); `link` is the tracker reference used to mirror it, and `source`
  (the path, else the link) is what work cut from the item remembers.
- `WorkSource.items(workspace) -> list[WorkItem]`: read-only.
- `Tracker(WorkSource)`: adds `id` and `publish(TrackerUpdate) -> TrackerReceipt`.
  `publish` must be safe to repeat: delivery is retried until it succeeds.
- `TrackerUpdate(kind, run, link, text, state, tickets)`: `specification`,
  `tickets` or `state`. Identical updates share an `id`.
- `TrackerReceipt(links)`: tracker references of items the delivery created
  (tickets), by factory run id. The factory stores them on the task records.
- `mirror_state(status, attention)`: the tracker state of a run, from the engine
  status and the board's attention code (a parent follows its children).

Core stays pure: these are dataclasses, protocols and codecs without IO.

## Outbox and delivery (`sdd_factory.tracking.TrackerSync`)

Mirroring is state-driven. Every 30 seconds (and on the `tracker-sync` action) the
application compares what each mirrored item should show with the newest recorded
publication and records only what changed, in the `tracker_outbox` table (schema
version 5), **before** any delivery. Delivery then sends a project's pending
publications oldest first. A failure keeps the order: the publication is retried
with a growing delay (30 s doubling up to an hour), nothing newer of the project
goes first, and after 12 attempts it is given up with its error kept. Mirroring
runs on its own thread, never under the queue's lock, so a slow tracker never
holds up scheduling. The flight log records `tracker_published` and `tracker_failed`.

Only items that came from the tracker (they carry a `link`) are mirrored. Features
and tickets get their link when the tracker created them, so a draft imported from a
Markdown file, or typed in, is not mirrored.

## Configuring a project

Project settings → **Tracker**: choose an installed adapter and write its settings
as `key: value` lines. Settings are stored with the project and **never hold a
secret**: keys such as `token`, `api_key`, `secret` or `password` are refused; name
the environment variable that holds the token in `token_env`. Through the HTTP API:

```json
{"id": "app", "name": "App", "workspace": "C:/app",
 "tracker": {"kind": "linear", "team": "ENG", "source": "projects",
             "token_env": "LINEAR_API_KEY"}}
```

## Linear (`sdd-trackers`, kind `linear`)

| Setting | Meaning | Default |
|---|---|---|
| `team` | Team key, for example `ENG` | required |
| `source` | `projects`: each unfinished project is a draft, its top-level issues the rows. `issues`: each unfinished issue with `label` is a draft, its sub-issues the rows | `projects` |
| `label` | Label that marks draft issues (`source: issues`) | `factory` |
| `states` | Workflow state names per mirror state, e.g. `{"needs_person": "In Review", "blocked": "Blocked"}` | first state of the matching type |
| `token_env` | Environment variable with a personal API key | `LINEAR_API_KEY` |
| `language` | Language of the text shown in Linear | the project's |

Row marks follow the issue's state type: completed `x`, canceled `-`, started `~`,
anything else open. Every publication carries a marker (`ffai:<id>`, and
`ffai-run:<run>` in created issues); delivery looks for it first, so a repeated
delivery never duplicates a comment or an issue.

The adapter is covered by a recorded GraphQL conversation (`tests/test_linear.py`);
it has **not** been qualified against a live Linear workspace yet. Try it on a test
team first.

## Adding a tracker

1. Implement `Tracker` from `sdd_core.tracking` in your own package: `items`,
   `publish`, a stable `id`. Depend on `sdd-core` only.
2. Register a factory taking the settings dict:

   ```toml
   [project.entry-points."sdd.trackers"]
   jira = "my_package.jira:JiraTracker"
   ```

3. Install it next to the application; the kind appears in project settings.
   Only installed adapters can be chosen; nothing is imported by name from
   settings or workflow text.

`sdd_trackers.graphql.GraphQL` is a small stdlib client adapters can reuse.
