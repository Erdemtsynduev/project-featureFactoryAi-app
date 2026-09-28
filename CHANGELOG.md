# Changelog

All libraries (`sdd-core`, `sdd-storage`, `sdd-runtime`, `sdd-providers`,
`sdd-workflows`, `sdd-usage`, `sdd-ui`) and the `feature-factory-ai` product are
released together with one version and pin each other exactly. The engine version
is shown in the UI rail, in **About the engine** and by `ffai --version`.
`tests/test_versions.py` rejects a release where versions or pins diverge.

A version bump follows Semantic Versioning: a breaking change to a published
contract (workflow schema, result schema, storage schema, HTTP actions) needs a new
minor version before 1.0 and a migration; existing tasks stay pinned to the workflow
digest they were created with.

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
