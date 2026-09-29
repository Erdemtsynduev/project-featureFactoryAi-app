# Feature Factory AI

This repository owns the independent Python engine, not production queues.
Core has no IO, wall clock, randomness, process or adapter dependencies.
All external effects are recorded before execution. Only the coordinator applies results.
Plugins are trusted installed code, explicitly registered, never imported from workflow text.
Do not report simulated fault time as a real 24-hour soak.
Do not migrate or start existing application queues during engine development.
Design every change to Clean Architecture, SOLID, DRY and KISS, and review your own
diff against them before reporting; refactor a violation in the same change:
- Domain rules are pure functions or value objects in `sdd_core` (or pure helpers
  such as `sdd_usage`); adapters and services only fetch, store and apply decisions.
- One owner per concept: vocabularies in `sdd_core.models`, documents through
  `codec.encode/decode`, one helper for one repeated rule (Python and JS alike).
- Small classes with one reason to change; depend on ports; extend by adding a
  row or a class, not by growing an if-chain. No speculative abstractions.
- Every behaviour change ships with tests; a fault or loop gets a simulation case.
Checks: `python tools/check_boundaries.py`, `python -m ruff check .`,
`python -m mypy`, `python -m pytest`. Use the repository virtual environment.

Commit workflow: use the repository-owned `.agents/skills/commit/SKILL.md` when
asked to commit. Validate every message with `tools/check_commit_message.py`.
Use configured user identity, Conventional Commits, and no tool/agent attribution.
Push only to the remote/branch explicitly authorized by the user.
