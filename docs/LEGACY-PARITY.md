# Legacy workflow comparison

This UI work inspected sdd-orchestrator's project registry, board, task drawer,
theme initialization, interview UX and main-flow reference prompts. It also
compared the role/state separation in mobile-sdd-factory's Constellation workspace.
Neither application nor any existing production queue was started or migrated.

| Capability | Current implementation | Boundary |
|---|---|---|
| Projects | Persistent registry, selector, filtering, response language and checks | Control DB stays outside workspaces |
| Kanban | Cards, drawer and pause/resume drag-and-drop | Acceptance and blockers remain engine decisions |
| Main flow | Detailed spec, tickets, implementation, checks, review, diagnosis, repair and reconcile prompts | Adapted to the current result protocol |
| Interrupted work | Confirmed termination routes declared recovery steps to read-only reconciliation | Uncertain termination retains quarantine |
| Recovery command | Reconciliation of the observed revision | Clears gates, stays paused; budgets and visit limits apply |
| Repair | Diagnosis and bounded repair, then checks/review again | No acceptance shortcuts |
| Questions | Versioned answers, drafts, discussion and inbox | Resolves an explicitly waiting human step |
| Guidance | Durable idempotent messages in future packets | No live stdin injection |
| Workers | Different desks, coffee/phone idle motion, mail on step change | Decorative, not a model reasoning or email indicator |
| Appearance | Persisted light/dark/system, RU/EN and response language | Existing content stays unchanged; arbitrary extension CSS cannot all be controlled |
| Usage | Local calls/tokens/active attempts, dispatch chart, Codex account probe | CLI presence is not auth proof; other quotas remain unavailable |
| Old plans | Read-only snapshot with paths, criteria and dependencies | Creates no executable tasks; states are historical |

The old autonomous portfolio audit/admission loop, grouped acceptance matrix,
automatic plan splitting, cross-plan reconciliation and queue refill are **not**
ported as production behavior. This engine works on explicit bounded requirements.
The tickets step produces a bounded implementation breakdown in a run; it does
not create an autonomous child queue. Legacy workflows require adaptation to
immutable definitions, recorded effects, resource ownership and acceptance gates.

Constellation uses persistent terminal roles. This runtime owns fresh processes
per attempt, so its UI reports messages as saved for the next invocation, not
acknowledged by a running model. Likewise, unknown quotas remain unknown instead
of being inferred from local token counts.

## Sources

- Local sdd-orchestrator README, INTERVIEW-UX, board/theme modules and
  sdd/main_flow/skills/orchestration-flow/references.
- Local mobile-sdd-factory README.
- [Codex app-server protocol](https://developers.openai.com/codex/app-server/):
  initialized JSONL connection and read-only account/rateLimits/read.
- [React Flow custom nodes](https://reactflow.dev/learn/customization/custom-nodes)
  and [taste-skill](https://github.com/leonxlnx/taste-skill) informed visual research.
  The shipped editor uses local SVG/CSS and adds no Node runtime.
