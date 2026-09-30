# Agent installations, profiles and models

The scheduler selects a named profile deterministically. It does not ask an LLM
which agent/model to use. Installation, authentication, catalog availability and
qualification are separate facts.

1. A **runner installation** supplies an adapter, absolute executable and optional
   interpreter arguments. This is machine-specific configuration.
2. An **adapter** implements the CLI protocol and catalog format (Claude, Codex,
   Cursor, OpenCode, or an explicitly installed extension).
3. A **profile** supplies the runner name, exact model identifier, permission policy
   and timeout. A workflow step names the profile.
4. A **model provider** belongs to the agent's native configuration. For OpenCode,
   the model is `provider/model`. Provider authentication stays in OpenCode.

## Discovery without model calls

```text
ffai agents discover
ffai agents discover --adapter cursor
ffai models list --runner local-opencode --config profiles.json
ffai profiles validate --config profiles.json --workflow flow.json
```

Discovery inspects PATH and adapter-owned installation patterns and makes bounded
`--version` calls. It reports missing/broken/installed/ambiguous; multiple versions
require selecting an explicit path. A version response is not proof of auth or
successful execution. Shell wrappers are not executed implicitly. Windows Cursor
can be represented as `node.exe` plus an absolute `index.js` argument. Linux/macOS
can use native CLI executables. Core/headless scheduling does not require Node.

Catalog commands use vendor-specific parsers, not guessed model names. Empty or
unrecognized output is an error with unknown availability. It does not prove an
account has no models. Catalogs are queried on demand and are not cached across
accounts. Authentication is performed using each CLI's native login commands;
the engine never copies credentials into a workflow.

## Configuration example

Replace executable and model placeholders before validation. This example
intentionally does not guess a Kimi model ID for an unverified OpenCode catalog.

```json
{
  "schema": 1,
  "runners": {
    "local-opencode": {
      "adapter": "opencode",
      "executable": "/absolute/path/to/opencode",
      "arguments": []
    }
  },
  "profiles": {
    "implement-kimi": {
      "runner": "local-opencode",
      "model": "<provider>/<exact-model-id>",
      "permissions": "workspace-write",
      "timeout_seconds": 900
    }
  }
}
```

On this Windows machine the observed OpenCode executable is
`C:/Users/erdem/AppData/Roaming/npm/node_modules/@opencode/cli/bin/opencode.exe`.
The installed version 2.0.16 returned exit 0 and zero output from `models`, both
with and without `--standalone`, on 2026-09-27. Its Kimi identifier is **unverified**.
Cursor's installed CLI returned `kimi-k3-low`, `kimi-k3-high`, `kimi-k3-max` and
`kimi-k2.7-code`. These are **confirmed catalog entries**, not successful execution
tests or a promise of account entitlement. A Cursor profile can use
`"model": "kimi-k3-max"` after pinning the discovered Cursor invocation.

A step uses `"kind": "agent", "profile": "implement-kimi", "mutates": true`.
Leave `handler` empty for a named profile. Existing application-specific handler/profile
configuration remains supported separately. OpenCode read-only execution is not
qualified; configuration rejects that policy. Claude/Codex/Cursor read-only
policies map to their existing adapter controls, not a universal security sandbox.

## Roles of a project

The bundled flows name four roles, and each project says which profile plays each
(project settings → **Роли агентов**, or `roles` in the `project` action):

| Role | Steps | Default profile |
|---|---|---|
| `lead` | revising a feature's tickets when one is stuck (`plan-review`) | `codex` |
| `analyst` | specification and ticket breakdown | `codex` |
| `implementer` | implementation, repair, conflict resolution | `claude` |
| `reviewer` | review, diagnosis, reconciliation | `codex` |

A role left out keeps its default. A change applies to work created afterwards; a
started run keeps the workflow version it was created with.

## Tools a step grants

Agents may edit files and, on working steps, run commands. Anything more is explicit
per step and recorded in the published, content-addressed workflow:
`{"tools": ["web"]}` in an agent step's config lets its agent search and fetch web pages
(Claude: `--allowedTools WebSearch,WebFetch`; Codex: `-c web_search="live"`). Only agent
steps may declare tools, only known tools are accepted, and publication refuses a
tool the bound handler's manifest does not list. A project's **web** setting grants the
web to every agent step of its ticket flow; planners and plan reviews are told which
steps have it.

## Upgrades apply between attempts
Profiles, models and agent CLIs may change at any time. A change applies from a run's next
attempt; an attempt in flight keeps the handler it started with, and its `handler.json`
records it. Nothing blocks a run because an agent was upgraded.

## Create a pinned run

```text
ffai --database test.db publish flow.json --config profiles.json
ffai --database test.db create DIGEST /project --id test --config profiles.json
ffai --database test.db resume test --version 0
ffai --database test.db supervise --config profiles.json
```

Creation resolves profiles into an immutable workflow snapshot and binds adapter
settings/executable hashes before returning the paused run. Editing a profile
after creation cannot change that run: a changed model, timeout, permissions,
installation or adapter binding blocks it. Create a new run to apply new settings.
`profiles validate --workflow` prints the effective definition without executing it.
Validation checks structure, compatibility and pinned files; it does not call a
model or assert that a provider account can serve the requested identifier.
`auto`, `default`, empty and placeholder model selections are rejected in named
profiles. There is no automatic model fallback.

## Extending adapters

Install trusted Python code with an `sdd.agents` entry point returning
`sdd_providers.catalog.AgentAdapter`, then explicitly list its entry-point name in
`agent_extensions`. The descriptor supplies invocation factory, discovery hints,
permission capability and optional catalog command/parser. The coordinator does
not branch on vendor names. Existing `sdd.handlers` and `sdd.executors` plugins
remain separate extension points. Workflow text never imports Python modules.

## UI configuration

Run `ffai --database /absolute/path/to/control/ui.db ui` and use the profiles tab.
The form creates the same configuration as the CLI; native authentication remains
in the selected tool. Profile changes do not silently rebind existing runs.
See [UI.md](UI.md) for editing and budget semantics.
