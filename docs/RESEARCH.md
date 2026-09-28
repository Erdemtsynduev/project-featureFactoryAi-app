# Research and provenance

Access date: 2026-09-27. Platform implemented and tested here: Windows, Python 3.14.7.
These are bounded findings, not a whole-product benchmark or proof of missing capabilities.

| Source | Evidence | Applied decision |
|---|---|---|
| Current sdd-orchestrator 0.5 | Local scheduler, workflow validator/engine, main-flow template, process host and Windows Job source read | Preserve revision gates, immutable workflows, explicit ownership and fresh sessions; consolidate execution paths |
| goodboy `ff47f9ec7d18e97a4e2ed60c49aee2f3c4b24a64` | Local context engine, workflow sequencer, architecture document | Provider-independent context and operation journaling |
| Seba `6b2ecef7a63b09ac542da85ba80d91fe9b363d10` | Local host composition and BudgetChecker | Separate host from providers; budget-check failure must not permit a new call |
| mobile-sdd-factory `fada813db944290d28b91b26c11700efa2bbfd43` | Local dispatch model, coordinator recovery and session policy | Attempt token, context identity and bounded recovery; no tmux dependency |
| [ORCA orchestration](https://www.onorca.dev/docs/cli/orchestration) | Official documented Run/Task/Dispatch model, ack deliveries, completion identity and worker release | Durable receipt identity and explicit resource ownership |
| [Ralph Orchestrator](https://github.com/mikeyobrien/ralph-orchestrator) | Official README describes event roles and quality gates | Extensible stages; transition authority remains deterministic |
| [Anthropic harness study](https://www.anthropic.com/engineering/harness-design-long-running-apps) | Official study describes bounded work, context handoff and independent evaluation | Fresh bounded activities and separately measured evaluation cost |

Confirmed means read in the indicated source. Applying those mechanisms here is our architectural
inference. Comparative reliability and token savings remain unverified: no matched workloads were
run against those products. ORCA's installed CLI could not be inspected because `orca` was not found.
The n8n node overview URL was unavailable; no unsupported implementation claim is based on it.

The new source is authored for this repository. No third-party runtime source, assets or proprietary
ORCA implementation was copied. Main Flow terminology and stage organization continue the existing
workspace's documented AI Hero influence; see the original workspace `orchestration/ORIGIN.md`.
Any future upstream source adoption must retain its original licence and provenance.
