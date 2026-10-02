# Milestone 6 completion (Prompts 1–6)

Dated 2026-10-02, branch `main` (uncommitted working tree; no commit, push,
deployment or production configuration change was made, and the checkout
`.env` was never loaded).

## What changed

| Area | Contract | Main files |
| --- | --- | --- |
| Recurring series (P1) | Anchored series, concrete occurrences with immutable (series, slot) identity, bounded expansion, this/future/all edits, exceptions | `app/planning/recurrence.py`, `series.py`, [recurrence.md](recurrence.md) |
| Manual placements (P2) | Moves are preserved until released; generation keeps history and manual work, never touches other dates, conflicts are atomic | `app/planning/workflow.py`, `application.py`, [execution-rescheduling.md](execution-rescheduling.md) |
| Sync recovery (P3) | Series precondition (`series_changed`), execution history never discarded on accept-remote, refused requests become conflicts, readable conflict view | `app/sync/engine.py`, `service.py`, `backend/resources.py`, [sync-protocol.md](sync-protocol.md) |
| Account recovery and protections (P4) | Single-use hashed recovery tokens, credential epochs end every older session, shared rate limits, body/host/CORS/timeouts | `backend/recovery*.py`, `rate_limit.py`, `protection.py`, [backend.md](backend.md) |
| Scheduling modes (P5) | Normal, ADHD, Early Finish, Night Owl, Catch-Up as objectives over one search; final evaluator | `app/mode_objectives.py`, `app/optimizer.py`, `app/planning/catch_up.py`, [scheduling-modes.md](scheduling-modes.md) |
| Integration (P6) | Cross-layer tests, comparison artifacts, documentation | `tests/planning/test_milestone6_integration.py`, `benchmarks/scheduling_modes_comparison.py` |

Migrations: local SQLite v9 (recurrence), v10 (manual placements), v11
(scheduling modes; rebuilds `preference_overrides` with rows, index and capture
triggers unchanged); server Alembic 0009, 0010, 0011 (recovery tokens,
credential epochs, rate-limit buckets), 0012 (mode checks). All are additive or
check-widening; nothing is back-filled by guessing. Rollout order: back up,
`python -m backend.migrate upgrade`, deploy the server, then update clients
(new clients negotiate `recurrence_occurrences`, `manual_placements` and
`scheduling_modes`). An access token issued before 0011 stays valid until its
account's first password reset.

## Requirement-to-test map

| Requirement | Tests |
| --- | --- |
| A. Recurrence expansion, identity, scoped edits, sync dedup | `tests/planning/test_recurrence_*.py`, `tests/sync/test_recurrence_sync.py`, `tests/backend/test_recurrence_api.py`, `tests/ui/test_recurrence_desktop.py`, `tests/execution/test_migration_v9.py` |
| B. Execution integrity, manual intent, scope, atomic regeneration | `tests/planning/test_manual_placements.py`, `test_explicit_rescheduling.py`, `test_rescheduling.py`, `test_workflow.py`, `tests/backend/test_manual_placements_api.py`, `tests/sync/test_manual_placement_sync.py`, `tests/execution/test_migration_v10.py` |
| C. Conflicts and interruption recovery | `tests/sync/test_conflict_recovery.py`, `test_protocol.py`, `test_rescheduling_sync.py`, `test_desktop_account_controller.py` |
| D. Recovery, sessions, limits, ownership | `tests/backend/test_account_recovery.py` (with the existing auth/isolation suites) |
| E. Five modes, scoring, Catch-Up evidence, wiring | `tests/test_scheduling_modes.py`, `tests/planning/test_milestone6_integration.py`, UI catalog tests |

## Comparison

- Normal/ADHD versus the pre-change engine:
  `python benchmarks/scheduling_mode_baseline.py --source-root . --check benchmarks/results/prompt1_scheduling_mode_baseline.json`
  reports **identical to the saved baseline** (captured from an isolated export
  of `ec072e7`, see `benchmarks/PROMPT1_SCHEDULING_BASELINE.md`).
- Five modes: `benchmarks/results/prompt5_scheduling_modes_comparison.json`
  and `.md` (runtime min/median/max over 5 runs, environment, B(S), stored
  insertion score, mode component, objective, scheduled/unscheduled counts and
  minutes with reasons, first start, last finish, idle gaps, fixed minutes,
  constraint violations -- 0 in every generated case; impossible required work
  is marked infeasible). Objectives of different modes are not one scale, and
  no global optimum is claimed.

## Verification

Commands run on Windows 10, Python 3.10.11 (project `.venv`):

- `python -m pytest` -- **1909 passed, 2 skipped** (PostgreSQL tests without `TEST_DATABASE_URL`; a
  symbolic-link test this Windows account cannot run).
- `python -m ruff check .` -- clean.
- `python -m compileall .` -- fails only on the git-ignored third-party
  `.diagnostic-deps` folder (Python 3.12 syntax); clean when it is excluded.
- PostgreSQL (`python -m pytest -m postgres tests/backend`,
  `BACKEND_TESTS_ON_POSTGRES=1`): **not run** -- no disposable
  `TEST_DATABASE_URL` was available, and no database or Docker service was
  started for it.
- Isolated desktop-only/direct-only dependency jobs from CI: not reproduced in
  separate environments.

## Not verified -- manual Windows checklist

No interactive Windows session was performed; automated geometry changes are
not physical dragging. **NOT VERIFIED.** Steps for the user:

1. Launch with disposable data; record Windows, Python, Tk and CustomTkinter versions.
2. Continuously drag and resize the Day, Calendar and Account windows while a
   recurring-series generation, a Catch-Up generation (history loading) and a
   slow sync (unplug the network or point at an unreachable backend) run.
3. Change pages, maximize/restore, use narrow and wide layouts, scroll large lists.
4. Switch accounts during a sync; open "Forgot password?" and close it while a request is in flight.
5. Close a page and exit the application while a worker runs.
6. Where available, move the window between monitors with different DPI.
7. Note any freeze, error dialog or visible latency.

## Operator configuration still needed

Placeholders only (see [backend.md](backend.md#protections)): `RECOVERY_PUBLIC_URL`
(https), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_SENDER`, `ALLOWED_HOSTS`, `CORS_ORIGINS`, `TRUSTED_PROXIES` (and the
matching uvicorn `--forwarded-allow-ips`), TLS termination, proxy body and time
limits, `DB_*` timeouts, `GENERATION_TIME_LIMIT_SECONDS`. Rate limits use the
server database, shared by every worker. **Recovery delivery was only mocked in
tests**; real SMTP delivery is unverified until an operator configures and
tests it. Direct PostgreSQL mode remains a private development setup: it holds
database credentials on the desktop and is not a secure distribution model.

## Remaining limitations

- Early Finish / Night Owl repacking is bounded (all orders up to 6 tasks,
  otherwise two orders); it never worsens the objective but is not globally
  optimal. Preserved placements count as busy time, not as movable work.
- Catch-Up evidence counts explicit completions and skips only (no "missed"
  outcome is inferred), per historical category snapshot.
- Overnight and DST-transition scheduling windows remain unsupported (explicit errors).
- Pure-expansion occurrences follow a changed series automatically; user
  exceptions under a changed series need a conflict decision.
