# Operational lessons and regression evidence

Review date: 2026-09-28. Sources: incident reports and runtime source from prior
deployments. Private source paths and archived research are in ignored
`reports/incident-review`; they are not runtime dependencies.

## Planning cost

**Confirmed in the incident report:** in a measured nine-hour window, 61 of 79
sessions performed audit or audit-review and consumed 81.6% of uncached input.
Background plan work was admitted while execution was blocked. This measures
input/session composition, not plan-correction necessity or subscription quota.
**Unverified:** reported 70% quota consumption; before/after provider quota
snapshots were unavailable.

**Architectural inference:** removing unsolicited portfolio-wide auditing and
bounding preparation separately should reduce avoidable calls. Actual savings
remain unmeasured until matched workloads are compared.

Implemented: approved-scope feature template without model planning, opt-in deep
planning, distinct counters, task caps and cumulative queue caps reserved in the
dispatch transaction. No model decides to start another planning pass. Custom
planning steps must be labelled explicitly; arbitrary prompts cannot be classified
deterministically by the engine.

## Incident matrix

| Failure | Mechanism | Evidence / remaining qualification |
|---|---|---|
| Background audits consume quota | Explicit tasks; separate planning/queue ceilings | `test_ui_and_planning.py`: pure budget, shared Memory/SQLite reservation and restart |
| cp1251 output failure | Host forces UTF-8 and attempt-local temp directories after overrides | Real Python child prints Cyrillic/CJK |
| Atomic replacement sharing violation | Bounded PermissionError retry; preserve old file on permanent failure | Transient and permanent fault injection |
| SQLite I/O failure acquiring a connection after process death | Configure persistent WAL at Store initialization; retry only pre-body acquisition on fresh connections, at most six attempts | `test_sqlite_acquisition.py`: injected WAL/setup/BEGIN failures, persistent failure propagation, no body/COMMIT replay; supervisor death test repeated five times with integrity and event replay checks |
| Process exits during Windows assignment | Check process signalled before and after failure | Completed real process and repeated live membership regression |
| Contained child denies stronger process rights | Limited-query membership then exact owned-job kernel PID query | Real kernel query plus injected OpenProcess denial; actual restricted-token ACL qualification pending |
| Shared workspace starts twice / blocked work starves queue | Durable claims, overlap checks, bounded admission, dependencies | Admission, storage-port and portfolio tests |
| Mutable checkpoint invalidates evidence | Checkpoint is transport data, not acceptance evidence | `test_checkpoint_policy.py`; historical data import outside library scope |
| Completion before acknowledgement / duplicate or stale reply | Durable receipts, generation/revision checks, transactional application | `test_faults.py`, core/store and recovery tests |
| Busy child tree outlives coordinator | Gated launch, owned containment, confirmed termination before retry | Process tests and prior 9h47 Windows soak; hostile escape unqualified |
| Disk full / changed evidence causes false acceptance | Rollback, durable ownership, revision and artifact hash gates | `test_faults.py`; UI cannot set accepted |
| Human answer races transition | Versioned answer, persisted before next dispatch | Stale-command and human-answer regression |
| Saved activity mistaken for health | Connection, queue intent, last transition and live errors separated | HTTP lifecycle; rendered-browser usability review pending |

Windows evidence does not imply Linux/macOS containment equivalence. Tests are
bounded reproductions, not proof that all production failures are eliminated.
Real subscription-reset payloads, network outages, restricted ACLs, hostile
process escape and OS endurance remain separate qualification work.

Kernel membership layout follows Microsoft's
[JOBOBJECT_BASIC_PROCESS_ID_LIST documentation](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list),
accessed 2026-09-28. No third-party implementation was copied for this change.

SQLite's [WAL persistence documentation](https://www.sqlite.org/wal.html#persistence_of_wal_mode)
confirms that the mode survives connection closure (accessed 2026-09-28).
The reported hosted-Windows failure happened before transaction execution; its
extended error code was not captured, so a transient OS cleanup race remains an
inference. Bounded retries do not classify all I/O errors as transient. Exhaustion
preserves the original exception and adds the extended code/name. Actual disk-full,
corrupt-database and post-BEGIN failures are not automatically replayed.
