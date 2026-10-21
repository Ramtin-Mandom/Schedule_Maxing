# Current state

A dated snapshot of what the repository does and how healthy it is. Update the relevant rows when a
feature's state changes, and do not rewrite the file only to move the date. Architecture is in
[ARCHITECTURE.md](ARCHITECTURE.md), planned work in [ROADMAP.md](ROADMAP.md), and file locations in
[CODEBASE_MAP.md](CODEBASE_MAP.md).

| | |
|---|---|
| Generated | 2026-10-21 |
| Commit | `bdc67f6` ("perf(ui): detach hidden pages only after 30 s hidden and when quiet") |
| Branch | `optimization`, ahead of `main` (`664de0e`, pull request #22: budgeting, updater edits, Ruff fix, certifi bump) by a knowledge-base sync and the round-3 performance work with its documentation ([docs/desktop-performance-round3.md](docs/desktop-performance-round3.md)) |
| App version | 1.2.0 (`app/version.py`); backend API version 1.0.0 (`backend/app.py`) |
| Local schema | SQLite v18 (`app/execution/db.py`); server Alembic head `0020_budget_future_items` |

**How to read the evidence.** *Verified* means a test or check was run in this session and passed.
*Verified failure* means a check was run and failed. *Code-inspected* means the implementation was read
but not run. *Unverified* means it could not be exercised here (live services, Windows installer builds,
PostgreSQL).

## A. Project overview

Schedule Maxing is a personal planner. The user records flexible tasks, fixed blocks and To Dos. A greedy
engine schedules a day in one of five modes. The user then marks work done, skipped or timed and earns
points, and the Productivity page summarises the results. Data is stored locally in SQLite and can sync to a
FastAPI/PostgreSQL account server.

| Subsystem | Operational? | Basis |
|---|---|---|
| Desktop app (local SQLite) | Yes | The dev suite passed except two tests that also fail before round 3 (section C); the affected real-window modules passed (section D) |
| Scheduling engines and modes | Yes | Engine, mode and baseline tests passed |
| Execution tracking and analytics | Yes | Execution and productivity tests passed |
| Sync client ↔ backend | Yes, against an in-process backend | `tests/sync` (system tier) passed in the full suite. No live server was contacted |
| Backend API | Yes on SQLite in tests | `tests/backend` passed in the full suite. PostgreSQL was not tested here |
| Hosted deployment (Render) | Unverified | `docs/ci-cd.md` §6: the deploy workflow has never been run against a real service |
| Windows installer and updater | Code-inspected; packaging config tests passed | No installer was built in this session |

## B. Feature implementation matrix

| Feature | Status | Implementation location | Evidence | Notes |
|---|---|---|---|---|
| Canonical day engine (minute-precise greedy) | Implemented | `app/optimizer.py: generate_day_schedule` | `tests/test_day_engine.py`, `test_optimizer_candidates.py`, `test_optimizer_differential.py` (dev suite, passed) | No global-optimum guarantee |
| Five scheduling modes | Implemented | `app/planning/preferences.py`, `app/mode_objectives.py`, `app/planning/catch_up.py` | `tests/test_scheduling_modes.py` (passed) | Normal, ADHD, Early Finish, Night Owl, Catch-Up |
| Greedy Optimizer v1 (legacy baseline) | Implemented | `app/optimizer.py: optimize_day_schedule` | `tests/test_optimizer.py` (passed) | Frozen; used by the legacy path and benchmarks |
| Week/month task-to-date allocation | Implemented | `app/planning/allocation.py`, `workflow.py` | `tests/planning/test_allocation*.py` (passed) | The Week and Month pages no longer have a range Make Schedule; `PlanningController.schedule_range` and the CLI still offer it |
| Freshness and provenance (stale detection) | Implemented | `app/planning/provenance.py` | `tests/planning/test_workflow.py` (passed) | Any input change in the range marks the day stale |
| Task form: Flexible / Fixed / To Do with points | Implemented | `app/ui/task_editor.py`, `task_form_model.py` | `tests/ui/test_task_form_model.py` (passed) | Since 1.1.0 there are no priority, deadline, dependency or repeat controls; existing values are kept and honored |
| Dependencies and deadlines | Partially Implemented | `app/pert.py`, `app/planning/allocation.py`, engine | `tests/test_pert.py`, planning tests (passed) | Enforced by the engine and by CSV import; the desktop form cannot set them (by design, since 1.1.0) |
| Recurring series | Partially Implemented | `app/planning/recurrence.py`, `series.py` | `tests/planning/test_recurrence_*.py` (passed) | Expansion, edits and sync work. The desktop form no longer creates new series (`docs/desktop-task-form.md`) |
| Projects and milestones | Implemented | `app/ui/planning_pages.py`, `projects_controller.py` | `tests/ui/test_projects_allocation.py`, `test_project_details.py` | Project archiving: Not Implemented |
| Manual placements and explicit rescheduling | Implemented | `app/planning/workflow.py: reschedule_placement`, `PlanningService.apply_reschedule` | `tests/planning/test_explicit_rescheduling.py`, `test_manual_placements.py` | |
| CSV import and export (legacy and canonical v2) | Implemented | `app/planning/csv_*.py` | `tests/planning/test_csv_*.py`, `tests/test_import_safety.py` | Legacy CSV rows always import as new records |
| Execution tracking (lifecycle, sessions) | Implemented | `app/execution/service.py`, `lifecycle.py` | `tests/execution/*` (passed) | |
| Fixed-block and To Do completion; Day board | Implemented | `app/execution/fixed_block_completion.py`, `app/ui/day_page.py` | `tests/ui/test_day_*.py`, `tests/execution/test_task_outcomes.py` | |
| Execution feedback (focus, energy, interruptions) | Partially Implemented | `ExecutionService.record_feedback`, `app/ui/execution_controller.py` | Code-inspected | `app/ui/feedback_dialog.py` exists but the running app never opens it |
| Productivity tracker (General / Specific / Project) | Implemented | `app/productivity/tracker.py`, `app/ui/productivity_page.py` | `tests/productivity/test_tracker*.py`, `tests/ui/test_productivity_page.py` | |
| Schedule follow-through (cohort) report | Partially Implemented | `app/productivity/schedule_cohort.py`; `GET /planning/analytics/schedule-cohort` | `tests/productivity/test_schedule_cohort*.py` | The desktop panel (`app/ui/cohort_view.py`) is not reachable, although `docs/desktop-day.md` still describes it |
| Historical duration suggestion (median) | Partially Implemented | `app/productivity/prediction.py`, `app/ui/duration_suggestion.py` | `tests/productivity/test_prediction.py`, `tests/ui/test_task_editor_widgets.py` | The widget is not used by the task form; the README still lists it as a feature |
| ML duration predictor | Implemented (CLI only, gated) | `app/productivity/ml_*.py`, `predictor_comparison_cli.py` | `tests/productivity/test_ml_*.py` | Kept out of the UI by design |
| Settings, preferences and task-data reset | Implemented | `app/ui/pages.py: SettingsPage`, `settings_controller.py`, `app/planning/task_data_reset.py` | `tests/ui/test_settings_*.py`, `tests/planning/test_reset.py` | |
| How to Use guide and About | Implemented | `app/ui/guide_page.py`; About is a `PlaceholderPage` holding the version text | `tests/ui/test_guide.py` | Settings, Account and How to Use share one sidebar item with a top bar (`app/ui/shell.py: GroupBar`) |
| Budgeting: balance (saved as today's; every manual change kept), income/expense entries, recurrence, occurrence edits, Stats with History and the 90-day forecast, Future (income ideas, wishlist) | Implemented (desktop) | `app/budget/`, `app/ui/budget_*.py`, `app/ui/day_money.py`, `app/persistence/budget.py`, `backend/resources.py` (`BUDGET_*`), local schema v17-v18, server revisions 0019-0020 | `tests/budget/*`, `tests/execution/test_migration_v17.py`, `test_migration_v18.py`, `tests/backend/test_budget_api.py`, `tests/sync/test_budget_sync.py`, `tests/direct/test_direct_budget.py`, `tests/ui/test_budget_controller.py`, `tests/ui/test_budget_page.py` | Local, direct and synced storage, REST CRUD; the local web profile has no budget routes |
| Local SQLite storage, migrations v1→v18, backups, instance lock | Implemented | `app/execution/db.py`, `backup.py`, `instance_lock.py` | `tests/execution/test_db.py`, `test_migration_v*.py`, `test_backup.py` | |
| Offline-first sync client | Implemented | `app/sync/` | `tests/sync/*` (full suite), `tests/test_sync_activity.py`, `tests/test_transport_keepalive.py` | Live server Unverified. Cadence follows the window's activity (60 s focused, 300 s otherwise, edits pushed 2.5 s after the last commit; ADR-041); one keep-alive HTTPS connection per transport |
| Desktop start-up and idle behaviour (round 3) | Implemented | `app/app.py`, `app/ui/pages.py` (`DeferredPage`, stepped pre-build), `app/ui/activity.py`, `app/ui/page_freshness.py`, `app/ui/shell.py` (page size, detached hidden pages), `app/ui/light_widgets.py`, `app/sync/credentials.py` (`LazyVault`), `app/planning/engine_process.py` | `tests/test_startup_cost.py`, `tests/ui/test_deferred_pages.py`, `test_activity*.py`, `test_page_freshness*.py`, `test_light_widgets.py`, `tests/test_engine_process.py` | Probe: first paint 5.5-5.8 s -> 1.1-1.2 s, minimized 60 s with no status poll, resize p95 330-460 -> ~100 ms, generation p95 ~15 ms; dwm.exe while dragging the Day page ~23% -> ~16% of a core ([docs/desktop-performance-round3.md](docs/desktop-performance-round3.md)). Physical dragging with a mouse Unverified |
| Conflict review and association UI | Implemented | `app/ui/account_page.py`, `conflict_view.py` | `tests/sync/test_desktop_account_*.py` | `keep_local` cannot be used on a record deleted on the server |
| Backend REST, planning API and sync push | Implemented | `backend/api.py`, `planning_api.py`, `sync.py` | `tests/backend/*` (full suite, SQLite) | PostgreSQL runs: CI `postgres` job only |
| Registration with email verification | Implemented | `backend/verification.py`, `verification_api.py` | `tests/backend/test_email_verification.py` | Live email delivery Unverified |
| Login, rotating refresh credentials, session revocation | Implemented | `backend/native_sessions.py`, `account_security.py`, `security_api.py` | `tests/backend/test_native_sessions.py`, `test_account_security.py` | |
| Two-step sign-in (TOTP and recovery codes) | Implemented | `backend/mfa.py` | `tests/backend/test_account_security.py` | |
| Password recovery and email change | Implemented | `backend/recovery.py`, `verification.py: EmailChangeService` | `tests/backend/test_account_recovery.py` | Needs a configured mail provider |
| Rate limiting, request limits, host/CORS policy | Implemented | `backend/rate_limit.py`, `login_guard.py`, `protection.py` | `tests/backend/test_auth_protection.py`, `test_server_security.py` | |
| Account deletion | Not Implemented | — | No route; `backend/maintenance.py` never deletes accounts | README lists it as not provided |
| Tombstone and change-log compaction | Not Implemented | — | README "Current Limitations" | |
| Direct PostgreSQL desktop mode (dev) | Implemented | `app/persistence/`, `app/ui/direct_services.py` | `tests/direct/*` (SQLite-backed where possible) | Not exercised against a real PostgreSQL here |
| Local web profile (loopback API) | Implemented | `app/web/` | `tests/web/*` | |
| Browser web UI | Not Implemented in this repository | — | No frontend sources; `docs/web-api.md` is its API contract | |
| Android client | Not Implemented | — | No Android sources | Mentioned only in the README author note and the skill |
| In-app updater | Implemented | `app/update/`, `app/ui/update_*.py` | `tests/update/*` | Live GitHub feed Unverified. Since pull request #22 the automatic check runs on every launch (no once-a-day limit), "Skip this version" is gone, and the update flow is built even when local storage cannot open; HTTPS setup for sync moves to the first request (Code-inspected) |
| Windows installer build and release workflow | Implemented | `packaging/windows/`, `.github/workflows/release.yml` | `tests/test_packaging_config.py`, `test_release_workflow.py` | No build was run here |
| Code signing | Not Implemented | Hooks in `build_windows.ps1` and `downloader.verify_signature` | `docs/windows-distribution.md` | Releases are unsigned |
| Backend continuous deployment | Unverified | `.github/workflows/deploy-backend.yml` | `tests/test_deploy_workflow.py` (configuration only) | Never run against Render (`docs/ci-cd.md`) |
| Scheduling across DST changes or overnight | Not Implemented | Refused in `app/planning/time.py` | Explicit errors | By decision (ADR-034) |

## C. Known bugs and limitations

| Issue | Severity | Affected | Evidence |
|---|---|---|---|
| An intermittent `TclError: invalid command name "tcl_findLibrary"` in one real-window test per run | Medium | Real-window test tier | Documented in `docs/testing.md` "Known flaky test"; cause unknown |
| UI modules that nothing reaches: `feedback_dialog.py`, `cohort_view.py`, `duration_suggestion.py`, and the `RewardConfigPage` class in `app/app.py` | Low (dead code and missing UI for supported features) | `app/ui/`, `app/app.py` | **Verified:** no production module imports or instantiates them (repository search) |
| Documentation contradicts the code: README says email verification is not provided; `docs/ci-cd.md` and README say CI runs on every push with four jobs; `docs/desktop-day.md` describes removed Productivity sections; the skill mentions Android | Low | Docs | **Verified** against `backend/verification*.py`, `ci.yml` (5 jobs, `pull_request` and `workflow_dispatch` only), `app/ui/productivity_page.py` |
| Legacy CSV loader splits a dependency name on hyphens | Low (legacy path only) | `app/data_processor.py` | Pinned as current behavior by `tests/test_data_processor.py` |
| `weight_category_bonus` is loaded but unused in scoring | Low | `app/reward.py` | Pinned by `tests/test_reward.py` |
| Simulated-annealing settings exist but no engine uses them | Low | `config/settings.py` | **Verified** by search |
| Reasons for unplaced tasks are not stored; after a restart only their count is known | Low | Day page | `docs/desktop-day.md` "Limitations" |
| Regeneration does not cancel the `scheduled` execution of a placement it replaces | Low | Rescheduling | `docs/execution-rescheduling.md` §10 |
| On DST days, timeline positions are elapsed minutes while labels show wall-clock times | Low | Day and Week views | `docs/desktop-day.md`, `docs/desktop-calendar.md` |
| `keep_local` is not possible for a record deleted on the server; diverged execution history must be resolved with `accept_remote` | Low (by design) | Sync | README "Current Limitations" |
| Desktop dependencies have lower bounds only, so a fresh install can get a newer CustomTkinter than the one tested | Low | `requirements-desktop.txt` | `docs/desktop-web-boundaries.md` §8 |
| Budget: one currency with 2 decimal places (not recorded per record); a series edit applies to the whole series (no "this and following" split); an occurrence can be moved only through the service (the forms change name, amount, category and project); a REST client's schedule edit leaves orphaned occurrence edits on the server (the expansion ignores them; the desktop service removes them) | Low (by design for now) | `app/budget/`, `app/ui/budget_page.py` | Module docstrings of `money.py`, `service.py`, `budget_controller.py` |
| Budget statistics read at most the last 3,660 days of history; the forecast's everyday spending is a flat 15-day average of one-time expenses (no seasonality) | Low (by design) | `app/budget/stats.py` | Module docstring |
| The Day page draws at most 40 money cards; more are reachable through the totals' dialogs | Low (by design) | `app/ui/day_money.py` | `MONEY_CARD_LIMIT` |
| The first sync of an account with a server that offers `budget` replays the whole change feed once (to pick up budget changes an older client version skipped) | Low (one-time cost) | Sync | `SyncEngine.pull_budget_history` |
| `tests/test_desktop_isolation.py::test_python_m_app_app_launches_offline_and_closes_cleanly` fails when start-up is slow: its probe closes the app 1.5 s after the main loop starts, and the first layout pass then ends inside CustomTkinter's title-bar update (`TclError: application has been destroyed`) | Low (test timing) | Launch probe (`tests/isolation_probe.py`) | **Verified failure** on 2026-10-20 on the clean commit `f20cf0b` as well (start-up took 10-18 s on this machine); it passed in the run of 2026-10-19, and **passed** on 2026-10-20 with the round-3 start-up (the constructor now takes about 0.7 s) -- possibly resolved, the probe's timing is unchanged |
| Two dev-suite tests fail on this machine: `test_a_balance_without_transactions_stays_flat` (the forecast's `zero_day` is None, the test expects tomorrow) and `test_new_tasks_get_distinct_types_and_never_group_by_name` (type label order) | Unknown (not investigated) | `app/budget/stats.py`, task types | **Verified failure** on 2026-10-21 on `257d0e8` and `bec7ad0` alike; on `bdc67f6` only the budget test failed (the task-types one looks order- or timing-dependent) |
| A page is built the first time it is shown (0.1-0.4 s once per page and workspace; Week is pre-built in idle time); after a window resize the shown page keeps its old size for 120 ms before it is laid out at the new one; returning to a page other than Day or the page left last (or to that one after 30 s) costs a re-layout of 0.2-0.7 s (it was detached into its own window) | Low (by design) | `app/ui/pages.py: DeferredPage`, `app/ui/shell.py: PAGE_SETTLE_MS`, `DETACH_AFTER_MS` | docs/desktop-performance-round3.md |
| With the opt-in Settings choice "Light caching" (ADR-045), only Day and the open section stay built: returning to another section rebuilds it (Week/Month ~0.8 s instead of ~60 ms), and its scroll position, unsaved input and Productivity's report cache are lost; in exchange ~15 MB less memory and ~2,200 fewer native windows after visiting every page | Low (by design, off by default) | `app/ui/shell.py: AppShell.set_light_caching`, `app/ui/pages.py: DeferredPage.release` | docs/desktop-performance-round3.md ("Addendum: light caching") |
| Dragging the window still costs dwm.exe work for every visible native child window (the Day page has ~435: form, buttons and navigation are CustomTkinter, 2-3 each) | Low | Desktop UI | docs/desktop-performance-round3.md "Round 3b", ADR-043 |
| While the window is not focused, remote changes arrive up to 5 minutes later (background sync interval 300 s) | Low (by design) | Sync | ADR-041 |
| Python 3.10, pinned everywhere, reaches upstream end of security support in October 2026 | Medium (suspected risk) | Toolchain | PEP 619 schedule; see ROADMAP R2 |

## D. Testing and quality

Test layout: 221 `test_*.py` modules under `tests/`, with tiers assigned automatically (`tests/test_tiers.py`).

| Command | Purpose |
|---|---|
| `python -m pytest -m dev` | Fast development suite: no real windows, system or slow tests |
| `python -m pytest` | Full suite, every tier |
| `python -m pytest -m ui` / `-m system` / `-m slow` | Real windows / sync, local web and direct mode / slow tests |
| `python -m pytest -m postgres tests/backend` | Needs `TEST_DATABASE_URL` pointing at a disposable PostgreSQL |
| `python -m compileall .` | Compile check |
| `ruff check .` | Lint |
| `python -m benchmarks.scheduling_mode_baseline --check` | Engine baseline drift (not run by CI) |

Results of the round-3 session (2026-10-20/21, Windows 10, `.venv` with Python 3.10.11, commit `bec7ad0`;
the round-3b rows are on `bdc67f6`):

| Check | Result |
|---|---|
| Round 3b, `pytest -m dev` on `bdc67f6` | **1 failed**, 2238 passed, 2 skipped, 397 deselected (222 s): the pre-existing `test_a_balance_without_transactions_stays_flat` (section C). The task-types test passed this time. `ruff check .` **Passed** |
| Round 3b, focused real-window and subprocess modules (light widgets, deferred pages and detaching, shell, freshness, Day page, task editor, settings, budget, calendar, planning, account page, desktop isolation and entry, engine process incl. a spawned-process comparison) | **Passed** per change; no full `-m "not dev"` run |
| `pytest -m dev` | **2 failed**, 2233 passed, 2 skipped, 388 deselected (218 s). The two failures (`tests/budget/test_budget_stats.py::test_a_balance_without_transactions_stays_flat`, `tests/planning/test_task_types_and_snapshots.py::test_new_tasks_get_distinct_types_and_never_group_by_name`) fail identically on `257d0e8`, before any round-3 change: **Verified failure**, not investigated |
| `ruff check .` | **Passed** |
| Real-window and system modules affected by round 3 (deferred pages, shell, settings, calendar, planning, productivity, Day, budget, task editor, async storage, account page and milestone-4 sync windows, desktop isolation, activity, page freshness) | **Passed:** 86, then 69 + 4, then 29 (per phase, not the full `-m "not dev"` tier) |
| `tests/sync/test_local_first_accounts.py`, `test_protocol.py`, `test_keep_signed_in.py`, `test_http_transport.py`, `tests/test_transport_security.py` | **Passed** |

Results of the session before (working tree including the then-uncommitted budgeting and updater changes):

| Check | Result |
|---|---|
| `pytest -m dev` | **Passed:** 2204 passed, 2 skipped, 371 deselected (204 s) |
| `pytest -m "not dev"` (system, slow and real-window tiers; with the above, the whole suite) | 370 passed, 1 skipped, 1 failed in 899 s: `tests/test_desktop_isolation.py` pinned the page list without Budgeting. After that expectation was updated the module **passed** (6 tests) |
| `python -m compileall` (app, backend, config, tests) | **Passed** |
| `ruff check .` | **Failed** on that tree (F841 at `app/ui/update_view.py:130`); fixed in `800996f`, and **passed** on `664de0e` (2026-10-20) |
| 2026-10-20 on `664de0e`: `pytest -m dev tests/update tests/budget tests/ui/test_budget_controller.py test_shell_foundation.py test_guide.py` | **Passed:** 188 |
| Earlier session: `tests/test_packaging_config.py`, `tests/test_release_workflow.py`; detect-secrets on the documentation | **Passed** (not re-run) |

Not run: the PostgreSQL-marked tests (no disposable database was available), the benchmarks, a Windows
installer build, and anything against live Render, GitHub or mail services.

Areas without automated coverage: the real deployment pipeline, signed-release verification (not
implemented), live email delivery, and behavior on a clean Windows machine after installation (a manual
checklist is in `docs/windows-distribution.md`).

## E. Infrastructure

| Area | State |
|---|---|
| Backend configuration | Environment variables only (`backend/settings.py`; names in [CODEBASE_MAP.md](CODEBASE_MAP.md#backend-configuration-names)). `.env.example` has placeholders. A local `.env` exists, is gitignored, and was not read |
| Databases | The desktop uses SQLite (`%LOCALAPPDATA%\ScheduleMaxing\executions.db`). The server uses PostgreSQL through 20 Alembic revisions. The ordinary test suite runs the server on SQLite |
| Default backend URL | `config.settings.DEFAULT_BACKEND_URL` points at a Render host. Availability was not checked |
| Deployment | Desktop: tag, then `release.yml`, then a GitHub Release, then the updater. Backend: `deploy-backend.yml` to Render, gated by `BACKEND_DEPLOY_ENABLED`; never run for real according to the docs |
| CI | `ci.yml`: `test`, `security-scan`, `desktop-only`, `direct-desktop`, `postgres`. Runs on pull requests and by hand. Results on GitHub were not checked in this session |
| Authentication | Implemented and tested in-process (section B). Not exercised against a live server |
| Synchronization | Implemented and tested against an in-process backend. Inert unless signed in |

## F. Immediate technical priorities

1. Confirm the CI result of pull request #22 on GitHub, then release (bump `app/version.py`, tag) ([ROADMAP](ROADMAP.md) M1).
2. Run and confirm the first real backend deployment, then the smoke check (M4).
3. Correct the documentation that contradicts the code (M2), and decide what happens to the unreachable UI
   modules (R3).
4. Find the intermittent Tk failure (M3).
5. Decide on account deletion, retention compaction (R1) and the Python version move (R2).
