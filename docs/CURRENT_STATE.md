# Current state

A dated snapshot of what the repository does and how healthy it is. Update the relevant rows when a
feature's state changes. See the [repository diagnosis](history/diagnosis.md),
[architecture](ARCHITECTURE.md), [roadmap](ROADMAP.md), and [feature map](FEATURE_MAP.md).

| | |
|---|---|
| Audited | 2026-11-23 |
| Commit | `fdd9b41` plus the 1.3.1 release preparation |
| Release preparation | 2026-11-24. PR #24 merged the engine remake and PR #25 merged repository cleanup and the tag-based backend workflow |
| App version | Source reports 1.3.1. The preceding published installer, `v1.3.0`, was built from `dfd53d6`. Backend API version is 1.0.0. [Published Windows releases](https://github.com/Ramtin-Mandom/Schedule_Maxing/releases) |
| Local and server schema | The 1.3.1 source supports SQLite v19 and Alembic `0021`. A read-only check confirmed the local desktop database is already on v19. The `v1.3.0` tag uses SQLite v18 and Alembic `0020`. [Cutover notes](backend/backend.md#priority-and-dependency-removal-0021) |

Current audit results appear first in section D. Earlier results keep their dates and revisions.

## A. Project overview

Schedule Maxing is a personal planner. The user records flexible tasks, fixed blocks and To Dos. A greedy
engine schedules a day in one of five modes. The user marks outcomes, earns points, and reviews them in the
Productivity page. Timed sessions are supported by the service and API. The desktop stores data in SQLite
and can sync to a FastAPI/PostgreSQL account server.

| Subsystem | Operational? | Basis |
|---|---|---|
| Desktop app (local SQLite) | Implemented | Local dev suite and GitHub `desktop-only` passed |
| Scheduling engine and five modes | Implemented | Dev tests, CLI sample and the scheduling-mode baseline passed |
| Execution and analytics | Implemented | Desktop outcomes and points, with timed sessions through the service and API. Dev tests passed |
| Sync client ↔ backend | Implemented | GitHub PostgreSQL job passed the backend, sync and direct-mode suites. Authenticated hosted behavior is unverified |
| Backend API | Implemented | GitHub SQLite and PostgreSQL jobs passed |
| Hosted deployment (Render) | Public endpoints reachable on retry | `/health` and `/ready` returned 200. The exact revision and new tag-triggered deploy path remain unverified |
| Windows installer and updater | 1.3.1 prepared | The release build and publish jobs previously passed for `dfd53d6`. The 1.3.1 build is the intended installer for SQLite v19. [Published Windows releases](https://github.com/Ramtin-Mandom/Schedule_Maxing/releases) |

## B. Feature implementation matrix

| Feature | Status | Implementation location | Evidence | Notes |
|---|---|---|---|---|
| Canonical day engine (minute-precise greedy) | Implemented | `app/optimizer.py: generate_day_schedule` | `tests/test_day_engine.py`, `test_optimizer_candidates.py`, `test_optimizer_differential.py` (dev suite, passed) | No global-optimum guarantee |
| Five scheduling modes | Implemented | `app/planning/preferences.py`, `app/mode_objectives.py`, `app/planning/catch_up.py` | `tests/test_scheduling_modes.py` (passed) | Normal, ADHD, Early Finish, Night Owl, Catch-Up |
| Greedy Optimizer v1 (legacy baseline) | Removed (2026-10-23, ADR-047) | — | — | The canonical day engine is the only engine |
| Week/month task-to-date allocation | Implemented | `app/planning/allocation.py`, `workflow.py` | `tests/planning/test_allocation*.py` (passed) | Required tier first, then points and deadline within each tier. Range generation is available through the planning API. The CLI previews ranges and generates one selected date |
| Freshness and provenance (stale detection) | Implemented | `app/planning/provenance.py` | `tests/planning/test_workflow.py` (passed) | Any input change in the range marks the day stale, a task's points included (`FINGERPRINT_VERSION` 2: every day saved before is stale once) |
| Task form: Flexible / Fixed / To Do with points | Implemented | `app/ui/task_editor.py`, `task_form_model.py` | `tests/ui/test_task_form_model.py` (passed) | Since 1.1.0 there are no priority, deadline, dependency or repeat controls; existing deadlines and repeat rules are kept and honored |
| Deadlines | Partially Implemented | `app/planning/allocation.py`, engine | planning and engine tests (passed) | Enforced by allocation and the engine; the desktop form cannot set them (by design, since 1.1.0) |
| Task dependencies and priority | Removed (2026-10-23, ADR-048) | — | `tests/execution/test_migration_v19.py`, `tests/backend/test_migrations.py` | Dropped from local schema v19 and server revision 0021; the sync wire still carries `priority` 5 and `dependency_ids` [] for 1.3.0 clients; CSV `priority`/`dependencies` columns import and are ignored |
| Recurring series | Partially Implemented | `app/planning/recurrence.py`, `series.py` | `tests/planning/test_recurrence_*.py` (passed) | Expansion, edits and sync work. The desktop form no longer creates new series (`docs/features/desktop-task-form.md`) |
| Projects and milestones | Implemented | `app/ui/planning_pages.py`, `projects_controller.py` | `tests/ui/test_projects_allocation.py`, `test_project_details.py` | Project archiving: Not Implemented |
| Manual placements and explicit rescheduling | Implemented in the service and API | `app/planning/workflow.py: reschedule_placement`, `PlanningService.apply_reschedule` | `tests/planning/test_explicit_rescheduling.py`, `test_manual_placements.py` | The Day timeline offers Release manual placement for a preserved move |
| CSV import and export (legacy and canonical format 3; format 2 still imports) | Implemented | `app/planning/csv_*.py` | `tests/planning/test_csv_*.py`, `tests/test_import_safety.py` | Legacy CSV rows always import as new records |
| Execution tracking (lifecycle, sessions) | Implemented in the service and API | `app/execution/service.py`, `lifecycle.py` | `tests/execution/*` (passed) | The desktop board offers outcome changes. Start, pause and resume have no current desktop entry point |
| Fixed-block and To Do completion; Day board | Implemented | `app/execution/fixed_block_completion.py`, `app/ui/day_page.py` | `tests/ui/test_day_*.py`, `tests/execution/test_task_outcomes.py` | |
| Execution feedback (focus, energy, interruptions) | Partially Implemented | `ExecutionService.record_feedback`, `app/ui/execution_controller.py` | Code-inspected | Recorded through the service and API; the desktop has no feedback dialog (the unused one was removed on 2026-11-04) |
| Productivity tracker (General / Specific / Project) | Implemented | `app/productivity/tracker.py`, `app/ui/productivity_page.py` | `tests/productivity/test_tracker*.py`, `tests/ui/test_productivity_page.py` | |
| Schedule follow-through (cohort) report | Implemented | `app/productivity/schedule_cohort.py`, `tracker.py`, `GET /planning/analytics/schedule-cohort` | `tests/productivity/test_schedule_cohort*.py` | Shared analysis feeds the tracker and API. The standalone desktop panel was removed in PR #25 |
| Duration prediction | Removed | — | [ADR-046](DECISIONS.md#adr-046--no-machine-learning-the-ml-duration-predictor-and-its-dependencies-are-removed), [ADR-051](DECISIONS.md#adr-051--retire-unreachable-desktop-presentations-and-duration-prediction) | The ML experiment and unused median estimator were removed. The tracker still compares estimates with recorded sessions |
| Settings, preferences and task-data reset | Implemented | `app/ui/pages.py: SettingsPage`, `settings_controller.py`, `app/planning/task_data_reset.py` | `tests/ui/test_settings_*.py`, `tests/planning/test_reset.py` | |
| How to Use guide and About | Implemented | `app/ui/guide_page.py`; About is a `PlaceholderPage` holding the version text | `tests/ui/test_guide.py` | Settings, Account and How to Use share one sidebar item with a top bar (`app/ui/shell.py: GroupBar`) |
| Budgeting: balance (saved as today's; every manual change kept), income/expense entries, recurrence, occurrence edits, Stats with History and the 90-day forecast, Future (income ideas, wishlist) | Implemented (desktop) | `app/budget/`, `app/ui/budget_*.py`, `app/ui/day_money.py`, `app/persistence/budget.py`, `backend/resources.py` (`BUDGET_*`), local schema v17-v18, server revisions 0019-0020 | `tests/budget/*`, `tests/execution/test_migration_v17.py`, `test_migration_v18.py`, `tests/backend/test_budget_api.py`, `tests/sync/test_budget_sync.py`, `tests/direct/test_direct_budget.py`, `tests/ui/test_budget_controller.py`, `tests/ui/test_budget_page.py` | Local, direct and synced storage, REST CRUD; the local web profile has no budget routes |
| Local SQLite storage, migrations v1→v19 (v19 needs SQLite 3.35+), backups, instance lock | Implemented | `app/execution/db.py`, `backup.py`, `instance_lock.py` | `tests/execution/test_db.py`, `test_migration_v*.py`, `test_backup.py` | |
| Offline-first sync client | Implemented | `app/sync/` | `tests/sync/*` (full suite), `tests/test_sync_activity.py`, `tests/test_transport_keepalive.py` | Live server Unverified. Cadence follows the window's activity (60 s focused, 300 s otherwise, edits pushed 2.5 s after the last commit; ADR-041); one keep-alive HTTPS connection per transport |
| Desktop start-up and idle behaviour (round 3) | Implemented | `app/app.py`, `app/ui/pages.py` (`DeferredPage`, stepped pre-build), `app/ui/activity.py`, `app/ui/page_freshness.py`, `app/ui/shell.py` (page size, detached hidden pages), `app/ui/light_widgets.py`, `app/sync/credentials.py` (`LazyVault`), `app/planning/engine_process.py` | `tests/test_startup_cost.py`, `tests/ui/test_deferred_pages.py`, `test_activity*.py`, `test_page_freshness*.py`, `test_light_widgets.py`, `tests/test_engine_process.py` | Probe: first paint 5.5-5.8 s -> 1.1-1.2 s, minimized 60 s with no status poll, resize p95 330-460 -> ~100 ms, generation p95 ~15 ms; dwm.exe while dragging the Day page ~23% -> ~16% of a core ([docs/history/desktop-performance-round3.md](history/desktop-performance-round3.md)). Physical dragging with a mouse Unverified |
| Light caching (opt-in, Settings -> Appearance; ADR-045) | Implemented | `app/ui/shell.py` (`set_light_caching`, `kept_pages`, `_release_when_quiet`), `app/ui/pages.py` (`DeferredPage.release`, Settings checkbox), `app/ui/ui_settings.py` (`light_caching`), `app/app.py` | `tests/ui/test_deferred_pages.py` (light caching tests), `tests/ui/test_shell_foundation.py` | **Verified** by those tests. Scratch probe: after every page, 3,246 -> 462 widgets, 89 -> 74-78 MB; returning to a page other than Day costs a first-visit build (section C) |
| Conflict review and association UI | Implemented | `app/ui/account_page.py`, `conflict_view.py` | `tests/sync/test_desktop_account_*.py` | `keep_local` cannot be used on a record deleted on the server |
| Backend REST, planning API and sync push | Implemented | `backend/api.py`, `planning_api.py`, `sync.py` | `tests/backend/*` (full suite, SQLite) | PostgreSQL runs: CI `postgres` job only |
| Registration with email verification | Implemented | `backend/verification.py`, `verification_api.py` | `tests/backend/test_email_verification.py` | Live email delivery Unverified |
| Login, rotating refresh credentials, session revocation | Implemented | `backend/native_sessions.py`, `account_security.py`, `security_api.py` | `tests/backend/test_native_sessions.py`, `test_account_security.py` | |
| Two-step sign-in (TOTP and recovery codes) | Implemented | `backend/mfa.py` | `tests/backend/test_account_security.py` | |
| Password recovery and email change | Implemented | `backend/recovery.py`, `verification.py: EmailChangeService` | `tests/backend/test_account_recovery.py` | Needs a configured mail provider |
| Rate limiting, request limits, host/CORS policy | Implemented | `backend/rate_limit.py`, `login_guard.py`, `protection.py` | `tests/backend/test_auth_protection.py`, `test_server_security.py` | |
| Account deletion | Not Implemented | — | `backend/maintenance.py` expires security and delivery records | Tracked in the roadmap |
| Tombstone and change-log compaction | Not Implemented | `backend/maintenance.py`, `backend/mutations.py` | Code-inspected | Retention currently covers security and email records. A sync retention policy needs recovery for old cursors |
| Direct PostgreSQL desktop mode (dev) | Implemented | `app/persistence/`, `app/ui/direct_services.py` | `tests/direct/*` (SQLite-backed where possible) | Not exercised against a real PostgreSQL here |
| Local web profile (loopback API) | Implemented | `app/web/` | `tests/web/*` | |
| Browser web UI | Not Implemented in this repository | — | No frontend sources; `docs/backend/web-api.md` is its API contract | |
| Android client | Not Implemented in this repository | — | No Android sources | The README author note records it as an original goal |
| In-app updater | Implemented | `app/update/`, `app/ui/update_*.py` | `tests/update/*` | Live GitHub feed Unverified. Since pull request #22 the automatic check runs on every launch (no once-a-day limit), "Skip this version" is gone, and the update flow is built even when local storage cannot open; HTTPS setup for sync moves to the first request (Code-inspected) |
| Windows installer build and release workflow | Implemented | `packaging/windows/`, `.github/workflows/release.yml` | GitHub `v1.3.0` build and publish passed | The 1.3.1 tag delivers the post-1.3.0 desktop changes |
| Code signing | Partial | Optional signing in `build_windows.ps1`; placeholder in `downloader.verify_signature` | `docs/deployment/windows-distribution.md` | The updater checks SHA-256 and has no Authenticode enforcement |
| Backend continuous deployment | Tag-triggered path unverified | `.github/workflows/deploy-backend.yml` | `tests/test_deploy_workflow.py`, GitHub Actions history | The prior merge-triggered workflow failed. No tag-triggered run is recorded. Render Auto-Deploy is an independent path |
| Scheduling across DST changes or overnight | Not Implemented | Refused in `app/planning/time.py` | Explicit errors | By decision (ADR-034) |

## C. Known bugs and limitations

| Issue | Severity | Affected | Evidence |
|---|---|---|---|
| An intermittent `TclError: invalid command name "tcl_findLibrary"` in one real-window test per run | Medium | Real-window test tier | Documented in `docs/development/testing.md` "Known flaky test"; cause unknown |
| Published 1.3.0 predates destructive server revision `0021` and local schema v19 | High | Upgrades, rollback and direct PostgreSQL mode | [Diagnosis](history/diagnosis.md#1-the-next-release-needs-a-coordinated-database-migration) |
| Desktop outcomes do not create timed sessions. Duration analytics depend on historical or API-created timed data | High product impact | Day, Productivity | `app/ui/day_outcomes.py`, `app/execution/service.py`, `app/productivity/tracker.py` |
| Range assignment uses aggregate free minutes and can pick a date without a contiguous slot even when another date fits | Medium | Range generation | Reproduced with an isolated in-memory workflow. [Diagnosis](history/diagnosis.md#4-range-allocation-can-miss-work-the-day-engine-could-place-elsewhere) |
| `weight_category_bonus` is loaded but unused in scoring (kept: it is stored in the preferences) | Low | `app/reward.py` | Pinned by `tests/test_reward.py` |
| Reasons for unplaced tasks are not stored; after a restart only their count is known | Low | Day page | `docs/features/desktop-day.md` "Limitations" |
| On DST days, timeline positions are elapsed minutes while labels show wall-clock times | Low | Day and Week views | `docs/features/desktop-day.md`, `docs/features/desktop-calendar.md` |
| A record deleted on the server or diverged execution history may require `accept_remote` | Low (by design) | Sync | [Sync protocol](architecture/sync-protocol.md) |
| Desktop dependencies other than CustomTkinter (capped below 6 since `b5668ff`) have lower bounds only, so a fresh install can get newer versions than the ones tested | Low | `requirements/desktop.txt` | `docs/architecture/desktop-web-boundaries.md` §8; ROADMAP R5 |
| Budget: one currency with 2 decimal places (not recorded per record); a series edit applies to the whole series (no "this and following" split); an occurrence can be moved only through the service (the forms change name, amount, category and project); a REST client's schedule edit leaves orphaned occurrence edits on the server (the expansion ignores them; the desktop service removes them) | Low (by design for now) | `app/budget/`, `app/ui/budget_page.py` | Module docstrings of `money.py`, `service.py`, `budget_controller.py` |
| Budget statistics read at most the last 3,660 days of history; the forecast's everyday spending is a flat 15-day average of one-time expenses (no seasonality) | Low (by design) | `app/budget/stats.py` | Module docstring |
| The Day page draws at most 40 money cards; more are reachable through the totals' dialogs | Low (by design) | `app/ui/day_money.py` | `MONEY_CARD_LIMIT` |
| The first sync of an account with a server that offers `budget` replays the whole change feed once (to pick up budget changes an older client version skipped) | Low (one-time cost) | Sync | `SyncEngine.pull_budget_history` |
| `tests/test_desktop_isolation.py::test_python_m_app_app_launches_offline_and_closes_cleanly` fails when start-up is slow: its probe closes the app 1.5 s after the main loop starts, and the first layout pass then ends inside CustomTkinter's title-bar update (`TclError: application has been destroyed`) | Low (test timing) | Launch probe (`tests/isolation_probe.py`) | **Verified failure** on 2026-10-20 on the clean commit `f20cf0b` as well (start-up took 10-18 s on this machine); it passed in the run of 2026-10-19, and **passed** on 2026-10-20 with the round-3 start-up (the constructor now takes about 0.7 s) -- possibly resolved, the probe's timing is unchanged |
| A page is built the first time it is shown (0.1-0.4 s once per page and workspace; Week is pre-built in idle time); after a window resize the shown page keeps its old size for 120 ms before it is laid out at the new one; returning to a page other than Day or the page left last (or to that one after 30 s) costs a re-layout of 0.2-0.7 s (it was detached into its own window) | Low (by design) | `app/ui/pages.py: DeferredPage`, `app/ui/shell.py: PAGE_SETTLE_MS`, `DETACH_AFTER_MS` | docs/history/desktop-performance-round3.md |
| With the opt-in Settings choice "Light caching" (ADR-045), only Day and the shown page stay built: returning to any other page (Settings <-> Account <-> How to Use too) rebuilds it (Week/Month ~0.8 s instead of ~60 ms), and its scroll position, unsaved input and Productivity's report cache are lost; in exchange ~15 MB less memory and ~2,200 fewer native windows after visiting every page | Low (by design, off by default) | `app/ui/shell.py: AppShell.set_light_caching`, `app/ui/pages.py: DeferredPage.release` | docs/history/desktop-performance-round3.md ("Addendum: light caching") |
| Dragging the window still costs dwm.exe work for every visible native child window (the Day page has ~435: form, buttons and navigation are CustomTkinter, 2-3 each) | Low | Desktop UI | docs/history/desktop-performance-round3.md "Round 3b", ADR-043 |
| While the window is not focused, remote changes arrive up to 5 minutes later (background sync interval 300 s) | Low (by design) | Sync | ADR-041 |
| Python 3.10 reached end of life on 2026-10-01, with a dependency-audit exception still in CI | High | Toolchain and dependency support | [PEP 619](https://peps.python.org/pep-0619/), [security operations](backend/security-operations.md#dependencies) |

## D. Testing and quality

Tests under `tests/` are assigned to tiers automatically by `tests/test_tiers.py`.

| Command | Purpose |
|---|---|
| `python -m pytest -m dev` | Fast development suite: no real windows, system or slow tests |
| `python -m pytest` | Full suite, every tier |
| `python -m pytest -m ui` / `-m system` / `-m slow` | Real windows / sync, local web and direct mode / slow tests |
| `python -m pytest -m postgres tests/backend` | Needs `TEST_DATABASE_URL` pointing at a disposable PostgreSQL |
| `python -m compileall .` | Compile check |
| `ruff check .` | Lint |
| `python -m benchmarks.scheduling_mode_baseline --check benchmarks/results/prompt1_scheduling_mode_baseline.json` | Engine baseline drift (not run by CI) |

Local cleanup results on `c030585` plus the working-tree changes (Windows, 2026-11-23):

| Check | Result |
|---|---|
| `pytest -m dev -q` | **1,958 passed, 3 skipped, 398 deselected**. The two existing Starlette and AnyIO deprecation warnings remain |
| Focused offline, direct and sync checks | **Passed**. The offline desktop subprocess, direct desktop workflow, direct lifecycle and the two-device execution history cases retained coverage after the old UI adapters were removed |
| Lint and scheduling baseline | **Passed**. The checked-in scheduling result is identical |
| Local runtime | The suite used a temporary Python 3.10.19 environment with the existing installed packages. The checkout's `.venv` points to a missing base interpreter |

The keep-alive regression test now waits for the closed peer to be readable before verifying the next
POST uses a new connection. Earlier runs could observe the response before the socket close arrived.

Pre-cleanup audit results on `c030585` (Windows, 2026-11-23):

| Check | Result |
|---|---|
| `pytest -m dev -q` | **1,994 passed, 3 skipped, 398 deselected**, two upstream test-client deprecation warnings. Temporary Python 3.10.19 used the checkout's installed packages because `.venv` points to a missing interpreter |
| Ruff, scheduling-mode baseline and `python -m app.main --demo` | **Passed**. Baseline identical |
| GitHub CI | All five jobs passed for [PR #24](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/40501046182) and [PR #25](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/41959633756), covering PostgreSQL 17, sync, desktop-only and the Linux real-window tests |
| Published Windows release | [1.3.0 build and publish passed](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/39890156928) on `dfd53d6`. A new Windows installer was outside this audit's local test scope |
| Public Render probes | Both endpoints timed out on the first attempt and returned 200 on retry. The public responses omit the server commit and schema revision |

Results of the engine-cleanup session (2026-10-23/24, Windows 10, `.venv` with Python 3.10.11, the tree committed
as `92c6993`):

| Check | Result |
|---|---|
| `pytest` (full suite, every tier) | **2 failed, 2389 passed, 2 skipped** (1009 s). The failures were `tests/ui/test_day_window_pages.py::test_week_and_month_bars_follow_the_selected_day` (the known `TclError` while Tk loads its library, section C) and `tests/test_transport_keepalive.py::test_a_connection_the_server_closed_is_replaced_before_the_next_request`; both **passed** when rerun alone |
| `pytest -m dev` (before the last test and doc edits) | **Passed:** 1992 passed, 2 skipped, 397 deselected |
| `ruff check .`; `python -m compileall` over `app backend benchmarks config packaging tests` | **Passed** |
| `python -m benchmarks.scheduling_mode_baseline --check benchmarks/results/prompt1_scheduling_mode_baseline.json` | **Identical** (results regenerated from a `git archive` export of `92c6993`; day-engine results equal the previous baseline, week results changed) |
| `tests/backend/test_migrations.py` on PostgreSQL (revision 0021) | **Unverified:** no PostgreSQL here; the CI `postgres` job is the first real run |

Results of the ML-removal session (2026-10-23, Windows 10, `.venv` with Python 3.10.11, `dfd53d6` plus the
uncommitted removal):

| Check | Result |
|---|---|
| `pytest -m dev` | **Passed:** 2183 passed, 2 skipped, 401 deselected, 2 warnings (228 s) |
| `pytest -m dev tests/test_packaging_config.py tests/test_startup_cost.py tests/ui/test_productivity_controller.py tests/productivity` | **Passed:** 213 |
| `tests/test_startup_cost.py::test_importing_the_desktop_loads_no_deferred_page` (slow) and `tests/backend/test_planning_api.py::test_generation_needs_no_desktop_packages` (system) | **Passed** |
| `ruff check .` | **Passed** |
| Pull request #23 CI on GitHub (`test`, `security-scan`, `desktop-only`, `direct-desktop`, `postgres`) | **Passed** (read with `gh`; the ML removal is not in CI yet) |

Results of the round-3 session (2026-10-20/21, Windows 10, `.venv` with Python 3.10.11, commit `bec7ad0`;
the round-3b rows are on `bdc67f6`):

| Check | Result |
|---|---|
| Light caching, `pytest -m dev` on the `f5d91aa` tree (2026-10-21) | **Passed:** 2239 passed, 2 skipped, 400 deselected (225 s). `ruff check .` **Passed** |
| Light caching, `tests/ui/test_deferred_pages.py` + `test_settings_page.py` + `test_shell_foundation.py` (real windows), again after the Day-and-shown-page refinement (2026-10-22, with `ruff check app tests`) | **Passed:** 17 (and 32 with the shell foundation module before the refinement) |
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

The historical checks above used the environments listed for each run. The current audit used isolated local
database probes and the linked GitHub results. It did not run the full suite locally or exercise hosted account
operations, live email delivery or a new installer upgrade.

Areas without automated coverage: the real deployment pipeline, signed-release verification (not
implemented), live email delivery, and behavior on a clean Windows machine after installation (a manual
checklist is in `docs/deployment/windows-distribution.md`).

## E. Infrastructure

| Area | State |
|---|---|
| Backend configuration | Environment variables only (`backend/settings.py`; names in [FEATURE_MAP.md](FEATURE_MAP.md#backend-configuration-names)). `.env.example` has placeholders. A local `.env` exists, is gitignored, and was not read |
| Databases | The desktop uses SQLite (`%LOCALAPPDATA%\ScheduleMaxing\executions.db`). Server Alembic head is `0021`. The ordinary suite uses SQLite for backend tests, with PostgreSQL 17 covered in CI |
| Default backend URL | `config.settings.DEFAULT_BACKEND_URL` points at Render. Public health and readiness responded on retry |
| Deployment | `release.yml` and `deploy-backend.yml` run independently on release tags. The backend workflow is gated by `BACKEND_DEPLOY_ENABLED`. Render Auto-Deploy is independent when enabled |
| CI | `ci.yml`: `test`, `security-scan`, `desktop-only`, `direct-desktop`, `postgres`. Runs on pull requests and by hand. Latest merged PRs verified green |
| Authentication | Implemented and tested in-process (section B). Not exercised against a live server |
| Synchronization | Implemented and tested against an in-process backend. Inert unless signed in |

## F. Immediate technical priorities

1. Identify the hosted revision and verify the backup, `0021` cutover and Windows installer upgrade before the
   next release. The engine remake and cleanup have already merged and passed CI.
2. Move the tested and packaged runtime off Python 3.10 and align dependency resolution with the audit lock.
3. Decide how the desktop should collect timed evidence and surface recurrence and deadline authoring.
4. Resolve the range-allocation blind spot and triage unused presentation adapters before adding scheduling modes.
5. Follow up on hosted account deletion and retention, updater signature enforcement, and the intermittent Tk test.
