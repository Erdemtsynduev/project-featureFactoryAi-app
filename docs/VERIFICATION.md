# Verification record

Date: 2026-09-27. Windows; Python 3.14.7. Results below distinguish executable
evidence from remaining qualification. Production projects and queues were not changed.

## Completed checks

| Check | Result |
|---|---|
| `python -m ruff check .` and formatting | PASS |
| `python -m mypy` | PASS, strict, 28 source modules |
| `python tools/check_boundaries.py` | PASS, package direction and pure-core imports |
| `python -m pytest -q` | 45 passed on final engine source |
| `python -m pytest tests/test_parallel.py -q` | 1 additional test passed: two parallel attempts, pause and restart |
| `python tools/build_check.py` | PASS: seven wheels, non-editable installation outside source tree, CLI and external plugin import |
| Final short wall-clock soak | PASS: 9 accepted runs, 1 injected host termination/recovery, zero model calls, 8.59 seconds |
| Real Codex protocol call | PASS: one bounded fresh read-only invocation, structured result, terminal state accepted |
| Real Claude protocol call | PASS: one bounded fresh read-only invocation, structured result, terminal state accepted |

The real calls were protocol smoke tests, not a real feature implementation benchmark.
Claude usage normalization was subsequently corrected using an offline regression fixture:
total input includes uncached input, cache reads and cache creation. The initial smoke token
numbers must not be used for a provider cost comparison.

Test coverage includes graph bypass, stale/duplicate results, receipt-before-ack recovery,
host-exit-before-collection recovery, actual coordinator/supervisor termination, descendant
cleanup, stop/pause preservation, unknown ownership, mocked disk-full transaction rollback,
evidence tampering, migration backup, dependency admission, external extension, profile pinning,
bounded waits, budgets, requirement closure, worktree integration and dirty-target conflicts.
Disk-full was injected with a SQLite abort trigger; the user's disk was not filled.

Build output: `reports/build-final.log`. Live records: `reports/live/<provider>/<run>/report.json`.
Source fingerprint of the final engine packages:
`fa7f65f9bf54b5a56ff45d796e43197093efe4568233045c5cbfe1e71931a50c`.

## Long-duration gate: RUNNING, not passed

Started a hidden 86400-second soak at 2026-09-27 19:01:38 UTC. Launch PID was 35256;
PID alone is not a lasting identity. The launch record includes its creation time.

- `reports/soak-24h.launch.json`: launched process identity.
- `reports/soak-24h/progress.json`: current status, accepted runs, injected recoveries,
  elapsed real time, model-call count and source fingerprint.
- `reports/soak-24h.stderr.log`: failure diagnostics.

Only a final `passed` report after at least 86400 elapsed seconds qualifies this gate.
Source changes abort the soak rather than mixing revisions. Sleep, reboot or process death
can interrupt it; a missing heartbeat is not success. Run a fresh full soak after such an interruption.

## Review findings fixed

- Closed SQLite probe/backup connections explicitly. A leaked probe reproduced a Windows WAL
  disk-IO failure immediately after coordinator termination; recovery tests now pass.
- Required editor insertion redirects repair paths as well as the happy path.
- Acceptance rechecks evidence through Engine itself, not only the coordinator front end.
- Preserved uncertain process reservations; job membership is confirmed empty before normal release.
- Frozen provider executable/model/plugin configuration before execution.
- Excluded build output from static typechecking and tested non-editable packages.
- Avoided rescanning every accepted run on each scheduler tick.

## Remaining limits

See ROADMAP.md for application-specific parity and application integration work. Automatic subscription-reset
parsing is not live-qualified, cross-platform support is not claimed, and the graph template is
not a drop-in migration of every application-specific portfolio preparation/repair contract. No production
deployment readiness or measured token savings is claimed while these gates remain open.
