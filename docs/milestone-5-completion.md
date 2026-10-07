# Milestone 5 completion record

Milestone 5 adds explicit rescheduling with preserved history, deterministic
planned-versus-actual analytics, and the desktop execution/history/productivity
workflow built on them. Greedy Optimizer v1 and the canonical day engine are
unchanged (the `--demo` schedule is identical; the optimizer regression and
differential suites pass).

## Delivered behavior

**Session 1 -- execution and rescheduling domain** ([execution-rescheduling.md](execution-rescheduling.md))

- One lifecycle table for SQLite, REST, direct PostgreSQL and sync (start,
  pause, resume, complete = Finish, skip, cancel); verified identically on all
  three storage paths.
- Explicit, validated, atomic reschedule of a never-started placement
  (`workflow.reschedule_placement`, `POST /planning/placements/{id}/reschedule`,
  sync action `reschedule`). The old placement becomes a tombstone with its
  original plan, `removal_reason = "rescheduled"` and `superseded_by_id`; its
  never-started execution is cancelled in the same transaction. Started,
  paused and terminal attempts are refused (`history_protected`).
- Placement provenance: `task_category` snapshot, `removal_reason`
  (`rescheduled`, `regenerated`, `deleted`, `task_deleted`, `reset`),
  `superseded_by_id`. Regeneration keeps unchanged placements and records
  supersession; every generation entry point protects started/finished work.
- Sync: the move is one idempotent operation whose result carries every
  changed record (`related`); competing moves, work started elsewhere and
  duplicate execution creation/starts give the loser a clear conflict. Plans
  moved or regenerated before a device's first sync are uploaded as history.

**Session 2 -- analytics** ([analytics.md](analytics.md))

- The existing terminal-outcome statistics are unchanged and labelled
  "among resolved executions".
- A schedule-cohort report over intended occurrences (placements with or
  without executions): due completion / skip rates with numerators and
  denominators, cancellations excluded and counted, future work apart,
  estimated vs actual (historical estimate), signed start delay and lateness,
  reschedule rate vs regeneration, category and time-of-day breakdowns,
  workload minutes by basis, underestimation groups by category or task
  identity, explainable day signals, and data-quality counts.
  `ProductivityService.build_schedule_cohort_report`,
  `GET /planning/analytics/schedule-cohort`.

**Session 3 -- desktop** ([desktop-day.md](desktop-day.md#uncompleted--tasks--completed))

- Execute tab: legal actions only (Start, Pause, Resume, Finish, Skip, Cancel,
  Reschedule...), derived overdue/late states, planned vs actual vs active
  time in the plan's timezone, honest "saved here / confirmed by the server /
  conflict" wording, no execution created by viewing, no duplicate on double
  clicks, conflicts reload the saved state.
- Productivity page: "Schedule follow-through" (the cohort), "History"
  (filters by dates, status, category; original plan, lineage, outcome,
  sessions -- also for removed work), and storage-accurate Data wording.

## Schema, API and service contracts

| Layer | Change |
| --- | --- |
| Local SQLite | Schema **v7**: `scheduled_tasks.task_category`, `removal_reason`, `superseded_by_id` (+ CHECKs, index); the unique index of `executions(scheduled_task_id)` narrowed to live rows. |
| Server | Alembic **0007**: the same placement columns on `placements` and `placement_revisions`, `ck_placements_removal_tombstone`, `ix_placements_user_superseded_by`, table `sync_operation_related_records`. |
| REST | `POST /planning/placements/{id}/reschedule`; `GET /planning/analytics/schedule-cohort`; placement records carry the three new fields; `DELETE /placements/{id}` records `deleted`. |
| Sync | Placement action `reschedule`; results may carry `related`; placement deletes carry `{removal_reason, superseded_by_id}`; a placement create with removal fields uploads history. |
| Services | `PlanningService.reschedule_source/apply_reschedule/schedule_history/placements_superseded_by`; `ProductivityService(..., history=, timezone_name=)`, `build_schedule_cohort_report`, `schedule_history_and_report`; `SyncService.record_sync_state`; `ExecutionController.perform/describe`; `PlanningController.reschedule_placement`; `ProductivityController.schedule_cohort_for_last/history/storage_copy`. |
| CSV v2 | Three optional placement columns appended; earlier files still import. |

## Metric rules (summary)

- Due completion = completed due occurrences / due non-cancelled occurrences
  (includes overdue-unattempted, in-progress, paused, skipped). Due skip rate
  uses the same denominator. Cancellations and future work are reported apart.
- One lineage (`superseded_by_id` chain) = one occurrence; its plan in effect
  at the cutoff attributes it to a local date; state is reconstructed at the
  cutoff from recorded instants. Nothing is completed by elapsed time.
- Local date ranges are half-open instant intervals in an explicit IANA
  reporting timezone (DST days are 23/25 hours); durations are UTC arithmetic.
- Missing values stay missing; a zero denominator is "unavailable" with a
  reason.

**Historical-data limitations**: placements and tombstones from before v7 /
0007 have no category snapshot and no removal reason (they count as "removed,
reason unknown", never as moves); past day windows are not recorded, so
historical capacity is unknown; legacy day-index executions appear only in the
terminal-outcome view.

## Migration and deployment

1. Back up. Run `python -m backend.migrate upgrade` (direct mode:
   `python -m backend.migrate --env-file .env upgrade`). 0007 is additive and
   safe while the previous server version runs; a failure rolls the whole
   upgrade back (tested).
2. Deploy the server, then update desktop clients (new clients need 0007;
   older clients keep working against it).
3. Desktop SQLite databases upgrade to v7 on open; a failed v7 leaves a usable
   v6 (tested).

Downgrades exist for tests and disposable databases only; never downgrade a
real database -- restore a backup instead.

## Verification (Windows 10, Python 3.10 project venv, real display, PostgreSQL 18 disposable cluster on 127.0.0.1)

| Check | Command | Result |
| --- | --- | --- |
| Full suite | `python -m pytest` | 1597 passed, 1 skipped (the PostgreSQL module: no `TEST_DATABASE_URL` in that run) |
| Compilation | `python -m compileall -q -x .venv .` | OK |
| Lint | `python -m ruff check .` | All checks passed |
| PostgreSQL-marked | `TEST_DATABASE_URL=... python -m pytest -m postgres tests/backend` | 14 passed |
| Backend/sync/direct on PostgreSQL | `BACKEND_TESTS_ON_POSTGRES=1 TEST_DATABASE_URL=... python -m pytest tests/backend tests/sync tests/direct` | 350 passed |
| Desktop-only environment | `requirements-desktop.txt` + pytest; web packages absent; `python -m pytest --ignore=tests/backend --ignore=tests/sync --ignore=tests/web --ignore=tests/direct` | 1229 passed |
| Direct-only environment | `requirements-direct.txt` + pytest; HTTP/JWT packages absent; `python -m pytest tests/direct tests/test_desktop_isolation.py` on PostgreSQL | 106 passed, 14 skipped (the REST/HTTP variants of the contract and analytics-adapter tests, which need FastAPI; they pass in the full environment and the PostgreSQL job) |
| CLI demo | `python -m app.main --demo` | exit 0; schedule identical |

The PostgreSQL database was a throwaway cluster created with `initdb` in a
temporary directory (trust authentication on loopback, database
`schedule_maxing_m5_test`) and removed afterwards; no personal or production
`DATABASE_URL` or `.env` was read. Display-backed widget tests ran on a real
Windows display (no Xvfb on Windows); CI runs them under `xvfb-run`.

### Defects found and fixed during verification

- A plan created and then moved or regenerated before a device's first sync
  lost its original plan and move on the server and other devices (the old
  "created and deleted offline" rule), so two devices disagreed on reschedule
  analytics. Such lineage tombstones are now uploaded as history and kept when
  pulled (tests: `test_a_plan_moved_and_regenerated_before_the_first_sync_keeps_its_lineage`,
  `test_history_uploads_are_tombstones_with_a_valid_successor`, the end-to-end test).
- Two new `tests/direct` modules imported FastAPI at module level, breaking
  collection in the direct-only environment; the HTTP parts are now imported
  lazily and skipped only where FastAPI is absent.

## Manual desktop walkthrough

1. `python -m app.app` (or `--storage postgres --env-file .env` for direct mode).
2. Day page: add two tasks for today, Make Schedule. Open the Execute tab:
   each item shows "Not started", its planned interval and timezone; nothing is
   saved yet.
3. Select one: Start, wait, Pause, Resume, Finish (add a note). Its active time
   excludes the pause. Close and reopen the app: the state is restored.
4. Select the other: Reschedule..., enter a free `HH:MM`. The timeline shows it
   at the new time; Productivity -> History shows the original plan and "moved".
5. Productivity: "Schedule follow-through" shows due completion as n/d with the
   date basis and timezone; the Data section says where history is stored.
6. With an account: after Sync now the Execute tab says "Confirmed by the
   server"; a second device shows the same history after syncing.

## Remaining limitations

- Historical capacity (the day window in effect on a past date) is not
  recorded, so overload signals use unfinished-workload thresholds only.
- A full regeneration may replace a manually moved placement (it is recorded
  as `regenerated`); incremental generation keeps it.
- Regeneration does not cancel the `scheduled` execution of a placement it
  replaces; analytics classify it by the tombstone's reason.
- REST has no op-id replay; clients reconcile by reading (`current`).
- Accepting the server's state over a failed local move restores the cancelled
  execution to its last acknowledged server state; feedback added to it after
  the move is not kept.
- The installed tz database decides DST (tzdata 2026.2 has British Columbia on
  permanent UTC-7 from March 2026).
