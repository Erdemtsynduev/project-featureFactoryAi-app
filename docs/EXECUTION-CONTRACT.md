# Execution contract and qualification boundary

The production SDD application is not migrated. This repository is an independent laboratory.

## Kernel and execution

`sdd_core.execution` exports `ExecutionRequest`, `ExecutionHandle`,
`ExecutionObservation`, and `ExecutionBackend`. No process, file, network, clock or
platform dependency enters this contract. A request contains a stable attempt ID,
generation, handler name, opaque payload and explicit deadline. A handle is not a PID.

An executor implements:

- `start(request)`: deduplicate identical requests; reject conflicting identity reuse.
- `reconcile(handle)`: running, completed, terminated or unknown.
- `cancel(handle)`: request cancellation; this is never proof of termination.

Completed observations carry a matching Result and finite completion timestamp. The
timestamp must fall between dispatch and observation and satisfy the attempt deadline.
Old generations cannot release ownership. Unknown executions retain their claim.
Started unfinished runs retain their workspace claim between steps. At dispatch
boundaries the coordinator orders eligible runs by their last dispatch,
so a repair loop yields a free slot to independent ready work.

`sdd_runtime.execution.ExecutionDriver` records immutable requests alongside the
outbox before invoking a backend. Call `Engine.dispatch`, then `driver.submit`, then
`driver.poll`. Callers hold the queue's coordinator Lease. After a lost start reply,
resubmit the SAME request; never allocate a replacement attempt. A remote service
must persist deduplication; the test HTTP server uses memory and is not a production service.

The local `Supervisor` and the HTTP adapter implement the same interface. An explicitly
installed `sdd.executors` entry point can add another implementation. The example
package depends only on core; its executor is intentionally volatile and returns
unknown after losing its memory.

The coordinator has no other launch path: it submits every attempt to its own
`Supervisor` through the driver and polls only that backend's attempts; an attempt
bound to another backend is left to that backend's driver. Attempts started by a
release before the supervisor are reconciled once on restore by their recorded host.

## Backend capabilities and limits

Windows uses Job Objects. POSIX uses dedicated process groups, flock and a parent
death watchdog. On both, the sandbox also ends descendants recorded by parent links
while the attempt ran (a packaged launcher's silent breakaway, `setsid`), including
after a coordinator restart. POSIX process groups are cooperative containment, not a
sandbox: a double-fork faster than a tick, hard resource limits and platform-specific
host death semantics still require qualification.

Claude working steps carry a PreToolUse hook (`sdd_providers.budget`) that refuses
background commands in the last quarter of the attempt's budget and every command in
the last tenth, telling the agent to return its result. Other providers get the
budget in their prompt only. An unresolved group blocks recovery.
The health projection reports this distinction and does not advertise Windows
resource limits on POSIX. Linux/macOS CI is configured but not run from this Windows session.

The HTTP adapter is a protocol test/reference client. It has timeouts, bounded
responses and strict identity validation; authenticated deployment, remote artifact
transport and production service persistence are not implemented by this client.

## Graphs and evidence

One active step per ticket; bounded cycles and conditional edges; a separate DAG
for ticket dependencies. No implicit parallel join semantics are introduced.
`insert_step` changes only explicitly selected edges. Use `additional_edges` to
atomically insert a required step on multiple incoming edges; each must share a
target. The validator rejects bypasses instead of silently changing other routes.

A failed gate removes its previous successful verdict. A mutating step invalidates
gates. Non-finite time cannot bypass deadlines. A lost attempt with model reservations
conservatively marks usage unknown; token totals do not imply zero expenditure.

An attempt's own files (packet, logs, receipt) are not workspace files. The engine
keeps them in its work folder, outside every project, and as evidence they are named
`.sdd-engine/<run-id>/<attempt-id>/<file>` wherever that folder is
(`sdd_core.sdk.evidence_name`); any other evidence is named relative to the workspace.
The current run's `.sdd-engine/<run-id>/checkpoint.md` is continuation state.
Normalized evidence names exclude only this exact checkpoint. Checkpoint-only
proof is rejected; other handoff/checkpoint files remain protected by hashes.
Legacy blocked-ticket migration and its one-time independent-review recovery remain
in the existing orchestrator; no live historical queue is imported here.

## Providers

Claude and Codex retain their existing adapters. Cursor/OpenCode support explicit
executable plus fixed argument prefixes (e.g. node + pinned index.js), immutable
invocation fingerprints, new sessions and fail-closed parsing. Provider session IDs
are recorded in results; no ambiguous continue-last option is used.

Cursor uses stream-json and ask mode for read-only tasks. Trust is an explicit
operator invocation option, not silently added by the adapter. Its real smoke passed
on Windows with an explicitly trusted empty test workspace.

OpenCode uses a private standalone server. Its read-only policy is unqualified and
read-only dispatch is refused. The real smoke exited successfully after about 40.3 seconds and returned a JSON
answer in a Markdown fence. The adapter rejected it because the stream lacked its
expected final step_finish(reason=stop) event. Its strict JSON parser would also
reject the Markdown wrapper. This was an adapter compatibility failure, NOT a
timeout. No second automatic model call was made. Synthetic fixture success is not live compatibility evidence.

Model accounting for these two new adapters stays unknown until normalized usage
has version-qualified coverage. Malformed output never triggers an automatic LLM
repair request. Session continuation and a terminal attach UI are not implemented.
