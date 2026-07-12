# Feature: Status-as-Projection (#1082)

> **Type**: reliability refactor / audit deliverable · P1 · `theme-reliability` · Epic #1045 (pull coordination)
>
> **One-line**: `schedule_executions.status` is a **CAS-guarded projection** of an execution's terminal event — in the backend, no code path reads `status='running'` as the standalone authority for "is this execution running?", and no writer can resurrect or clobber a terminal row. (Scheduler-process writers are a tracked [follow-up gap](#known-gap--scheduler-writers-1082-follow-up).)

## Overview

The single fact *"is execution X running?"* is physically split across three stores:

| Store | Key | Role |
|-------|-----|------|
| Redis slot ZSET | `agent:slots:{name}` | capacity coordination (ephemeral) |
| SQL row | `schedule_executions.status` | durable projection / display |
| Agent RAM | agent-server process registry (`/api/executions/running`) | **runtime authority** |

Because no single store was *declared* authoritative, the platform runs continuous reconciliation machinery (the cleanup-service sweeps + the canary S-01/S-02/S-03 invariants) to keep them in sync. #1082 — "bankable reliability win #1" from the 2026-06-05 coordination review — collapses *"is running"* to a **single owner** so split-brain is removed structurally, and codifies `status` as a projection that no reader treats as the standalone authority.

**This is not** the full push→pull migration (#1081), the `ExecutionStateProjector`/shadow-column rewrite, or removal of the slot ZSET / cleanup pyramid (#429 / #1081 Phase 5). The acceptance bar is **"no behavior change — projection semantics only."** It lands cleanly *before* pull so the migration inherits a single-owner status.

## The single-owner contract

1. **The agent's process registry is the runtime authority** for "is running." A backend reader may use `status='running'` as a *candidate filter* (which rows to ask about), but must confirm against the agent registry (or, for slot reclaim, the terminal/missing SQL row) before any destructive write.
2. **`status` is a CAS-guarded projection of the terminal event.** In the **backend `db/schedules.py` module**, every `update(schedule_executions)` that writes `status` carries a status precondition in its `WHERE` (or is the atomic claim whose precondition is its sub-query), so a stale or duplicate writer is a no-op against a row that has already moved on. The execution's own terminal result wins (`update_execution_status`, RELIABILITY-005 / #524); a user cancel is authoritative over a late agent "success" (#671). (The standalone scheduler process is a separate writer not yet covered — see [Known gap](#known-gap--scheduler-writers-1082-follow-up).)
3. **No phantom reversal.** A terminal row (`success`/`failed`/`cancelled`/`skipped`) can never be moved back to a non-terminal state. This is the E-02 invariant the canary guards.

## CAS gap closed (Step 1)

`db/schedules.py:update_execution_to_queued` — the overflow re-queue path (`backlog_service.enqueue`) — previously wrote `status=QUEUED` with `WHERE id=?` and **no status precondition**. A stale/duplicate call could resurrect a terminal row into `queued` (the E-02 phantom-reversal class). It now carries `AND status == RUNNING` (mirroring the sibling `release_claim_to_queued`), so only a currently-running row — the state the row is in when the slot acquire fails — may spill into the backlog. The caller treats a `False` return ("row gone or already terminal") as a clean rejection; no slot is held on that path.

## Reader audit — every backend `schedule_executions.status` reader

Scope: the **backend process** (`src/backend/`). The audit found **no backend reader that acts destructively on `status='running'` as the sole authority.** "Remove any reader that treats status as authoritative" therefore resolves to *codify the existing discipline*, not re-point working code. (The scheduler process is audited separately — see [Known gap](#known-gap--scheduler-writers-1082-follow-up).) Backend readers fall into three classes:

### 1. Authority-crosschecking (cleanup watchdog)

Read `status='running'` (or a reclaimed Redis slot) only as a **candidate filter**, then confirm against the agent registry / Redis before any destructive write. **Compliant** — status is not the standalone authority. Each site carries a `#1082` "candidate filter only" comment.

| Site (`services/cleanup_service.py`) | Candidate | Confirmed against | Destructive write |
|--------------------------------------|-----------|------------------|-------------------|
| `_reconcile_orphaned_executions` | `db.get_running_executions_with_agent_info()` | agent `/api/executions/running` (incl. #921 recently-completed window) | `mark_execution_failed_by_watchdog` (race-guarded) |
| `_process_stale_slot_reclaims` | TTL-reclaimed slots | just-in-time agent re-verify (#378) | `fail_stale_slot_execution` (race-guarded `WHERE status='running'`) |
| `_reconcile_orphaned_slots` | Redis slot members (#749) | SQL row terminal/missing + grace window | `ZREM` the orphan slot only (never fails a running execution; a non-terminal row *protects* its slot) |

### 2. Race-guard writers (the projection guard itself)

Use `status` defensively in the `WHERE` to avoid clobbering a terminal row. **Compliant** — this *is* the CAS projection guard.

`update_execution_status` (RELIABILITY-005), `update_execution_to_queued` (#1082, Step 1), `release_claim_to_queued`, `claim_next_queued` (atomic claim, sub-query precondition), `cancel_queued_execution`, `cancel_queued_for_agent`, `fail_queued_for_agent`, `expire_stale_queued`, `mark_stale_executions_failed`, `mark_no_session_executions_failed`, `fail_stale_slot_execution`, `finalize_orphaned_skipped_executions`, `mark_execution_failed_by_watchdog`.

### 3. Display / reporting (read-only)

Read `status` only to render — **out of scope**, projections are fine to display.

`get_execution`, `get_schedule_executions`, `get_agent_execution_stats`, `get_agent_analytics`, and the executions / agents / public routers.

## Regression guard (Step 2)

`tests/unit/test_schedule_status_observability.py`:

- **Static AST guard** (`TestStatusWriteProjectionGuard`) — parses `db/schedules.py`, enumerates every `update(schedule_executions)` site, and asserts (a) the inventory matches a curated allowlist (a *new* update site fails CI until classified) and (b) every status writer references `schedule_executions.c.status` in a predicate. A meta-test proves the detector fires on the pre-#1082 unguarded shape. This catches a future blind write even if no one hand-writes a behavioural test for it. **Blind spots (by design — it's a tripwire, not a proof):** the guard is *file-scoped* to `db/schedules.py` (it cannot see the scheduler's raw-SQL writers), recognizes only the `update(schedule_executions)` call shape (not `text()` raw SQL, executemany, or an aliased table), and treats any `schedule_executions.c.status` comparison in the function as sufficient (a check-then-blind-update would pass). The blind spots are enumerated in the test's module docstring.
- **Behavioural no-op proofs** (`TestStatusWriteNoOpOnTerminalRow`) — drive a real row to each terminal state and assert each writer is a no-op (Step 1 gap covered explicitly: re-queue against `success`/`failed`/`cancelled`/`skipped` must not resurrect the row; the happy path RUNNING→QUEUED still works).

## Canary S-01 disposition (Step 4)

`canary/invariants/s01_slot_row_bijection.py` (slot–row bijection) is **downgraded `critical` → `major`** and annotated as redundant under single-owner status: with `status` a CAS-guarded projection, the slot ZSET is no longer a competing authority — only an ephemeral coordination hint. S-01 stays **registered and Tier-A** (it still catches real slot-ZSET/SQL drift while the push-model ZSET exists) and only *retires* with the slot ZSET in #1081 Phase 5. Matches the E-05 Tier-A downgrade precedent.

## Known gap — scheduler writers (#1082 follow-up)

The single-owner contract above is enforced in the **backend** process. The standalone **scheduler** (`src/scheduler/`, `trinity-scheduler` container) writes the *same* `trinity.db` and is **not** yet CAS-guarded:

- `scheduler/database.py::update_execution_status` — `UPDATE … SET status=? … WHERE id=?` (no status precondition).
- `scheduler/database.py::schedule_retry` — `UPDATE … SET status='pending_retry' WHERE id=?` (no status precondition).

The cron-path failure handler compensates with a **non-atomic read-then-check** (`scheduler/service.py`, SCHED-ASYNC-001: re-read `status`, skip the write if no longer `running`), so the common path behaviourally honors the projection. But the **retry-failure path** (`scheduler/service.py` `_run_retry`) writes `FAILED` with no such check, and `schedule_retry` can move a terminal row to `pending_retry`, so a late backend `SUCCESS` racing either write can still be reversed (the E-02 class, in the scheduler process). This is **pre-existing** and outside #1082 Step 1's backend scope. A complete fix mirrors RELIABILITY-005: add `AND status='running'` / `status NOT IN (terminal)` to the scheduler SQL (or the same read-then-check on the retry path). Tracked as a #1082 follow-up.

## Related

- [capacity-management.md](capacity-management.md) — `CapacityManager` admit/release; overflow → `backlog_service`
- [persistent-task-backlog.md](persistent-task-backlog.md) — BACKLOG-001 enqueue/drain (home of `update_execution_to_queued`)
- [cleanup-service.md](cleanup-service.md) — the authority-crosschecking watchdog readers
- [task-execution-service.md](task-execution-service.md) — single execution path; records every terminal outcome
- [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md) — `fail_queued_for_agent` on breaker `→open`
- Canary S-01/S-02/S-03 — `docs/memory/architecture.md` Canary Invariant Harness table
