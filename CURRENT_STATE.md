# Current state

A dated snapshot of what the repository does and how healthy it is. Update the relevant rows when a
feature's state changes, and do not rewrite the file only to move the date. Architecture is in
[ARCHITECTURE.md](ARCHITECTURE.md), planned work in [ROADMAP.md](ROADMAP.md), and file locations in
[CODEBASE_MAP.md](CODEBASE_MAP.md).

| | |
|---|---|
| Generated | 2026-10-19 |
| Commit | `6cc8855b2ca423a0b1f092f515dcb243f9276f4e` ("Release 1.2.0") |
| Branch | `codex/debug-1.2.0-update`, with uncommitted changes in 11 tracked files (updater UI, sync transport, UI settings, the build script's Python check, and their tests) |
| App version | 1.2.0 (`app/version.py`); backend API version 1.0.0 (`backend/app.py`) |
| Local schema | SQLite v16 (`app/execution/db.py`); server Alembic head `0018_account_security` |

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
| Desktop app (local SQLite) | Yes | The dev suite passed, including desktop controllers and widgets. The real-window tier passed in the full suite (section D) |
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
| How to Use guide and About | Implemented | `app/ui/guide_page.py`; About is a `PlaceholderPage` holding the version text | `tests/ui/test_guide.py` | |
| Local SQLite storage, migrations v1→v16, backups, instance lock | Implemented | `app/execution/db.py`, `backup.py`, `instance_lock.py` | `tests/execution/test_db.py`, `test_migration_v*.py`, `test_backup.py` | |
| Offline-first sync client | Implemented | `app/sync/` | `tests/sync/*` (full suite) | Live server Unverified |
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
| In-app updater | Implemented | `app/update/`, `app/ui/update_*.py` | `tests/update/*` | Live GitHub feed Unverified; the debug branch is changing it |
| Windows installer build and release workflow | Implemented | `packaging/windows/`, `.github/workflows/release.yml` | `tests/test_packaging_config.py`, `test_release_workflow.py` | No build was run here |
| Code signing | Not Implemented | Hooks in `build_windows.ps1` and `downloader.verify_signature` | `docs/windows-distribution.md` | Releases are unsigned |
| Backend continuous deployment | Unverified | `.github/workflows/deploy-backend.yml` | `tests/test_deploy_workflow.py` (configuration only) | Never run against Render (`docs/ci-cd.md`) |
| Scheduling across DST changes or overnight | Not Implemented | Refused in `app/planning/time.py` | Explicit errors | By decision (ADR-034) |

## C. Known bugs and limitations

| Issue | Severity | Affected | Evidence |
|---|---|---|---|
| Ruff fails on the working tree: F841, `release` assigned but unused | Medium (fails the CI `test` job) | `app/ui/update_view.py:130` (uncommitted edit) | **Verified failure:** `ruff check .` reported 1 error |
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
| Python 3.10, pinned everywhere, reaches upstream end of security support in October 2026 | Medium (suspected risk) | Toolchain | PEP 619 schedule; see ROADMAP R2 |

## D. Testing and quality

Test layout: 201 `test_*.py` modules under `tests/` (about 2,460 collected tests), with tiers assigned automatically (`tests/test_tiers.py`).

| Command | Purpose |
|---|---|
| `python -m pytest -m dev` | Fast development suite: no real windows, system or slow tests |
| `python -m pytest` | Full suite, every tier |
| `python -m pytest -m ui` / `-m system` / `-m slow` | Real windows / sync, local web and direct mode / slow tests |
| `python -m pytest -m postgres tests/backend` | Needs `TEST_DATABASE_URL` pointing at a disposable PostgreSQL |
| `python -m compileall .` | Compile check |
| `ruff check .` | Lint |
| `python -m benchmarks.scheduling_mode_baseline --check` | Engine baseline drift (not run by CI) |

Results in this session (Windows 10, `.venv` with Python 3.10.11, working tree including the uncommitted
changes):

| Check | Result |
|---|---|
| `pytest -m dev` | **Passed:** 2103 passed, 2 skipped, 358 deselected (189 s) |
| `pytest` (full suite, every tier, real windows included) | **Passed:** 2461 passed, 2 skipped, 5 warnings (dependency deprecations and a JWT key-length warning in a test) in 1161 s. The documented `tcl_findLibrary` failure did not occur in this run |
| `python -m compileall` (app, backend, config, packaging, tests, benchmarks) | **Passed** |
| `ruff check .` | **Failed:** 1 error, F841 at `app/ui/update_view.py:130` |
| Focused: `tests/test_packaging_config.py`, `tests/test_release_workflow.py` | **Passed:** 47 |
| detect-secrets on the new documentation files, against `secret-scan-baseline.json` | **Passed** |

Not run: the PostgreSQL-marked tests (no disposable database was available), the benchmarks, a Windows
installer build, and anything against live Render, GitHub or mail services.

Areas without automated coverage: the real deployment pipeline, signed-release verification (not
implemented), live email delivery, and behavior on a clean Windows machine after installation (a manual
checklist is in `docs/windows-distribution.md`).

## E. Infrastructure

| Area | State |
|---|---|
| Backend configuration | Environment variables only (`backend/settings.py`; names in [CODEBASE_MAP.md](CODEBASE_MAP.md#backend-configuration-names)). `.env.example` has placeholders. A local `.env` exists, is gitignored, and was not read |
| Databases | The desktop uses SQLite (`%LOCALAPPDATA%\ScheduleMaxing\executions.db`). The server uses PostgreSQL through 18 Alembic revisions. The ordinary test suite runs the server on SQLite |
| Default backend URL | `config.settings.DEFAULT_BACKEND_URL` points at a Render host. Availability was not checked |
| Deployment | Desktop: tag, then `release.yml`, then a GitHub Release, then the updater. Backend: `deploy-backend.yml` to Render, gated by `BACKEND_DEPLOY_ENABLED`; never run for real according to the docs |
| CI | `ci.yml`: `test`, `security-scan`, `desktop-only`, `direct-desktop`, `postgres`. Runs on pull requests and by hand. Results on GitHub were not checked in this session |
| Authentication | Implemented and tested in-process (section B). Not exercised against a live server |
| Synchronization | Implemented and tested against an in-process backend. Inert unless signed in |

## F. Immediate technical priorities

1. Fix the Ruff error and get the debug branch green through a pull request ([ROADMAP](ROADMAP.md) M1).
2. Run and confirm the first real backend deployment, then the smoke check (M4).
3. Correct the documentation that contradicts the code (M2), and decide what happens to the unreachable UI
   modules (R3).
4. Find the intermittent Tk failure (M3).
5. Decide on account deletion, retention compaction (R1) and the Python version move (R2).
