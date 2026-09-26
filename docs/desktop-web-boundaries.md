# Desktop and web boundaries (Milestone 4, Prompt 0)

The earlier Milestone 4 prompts 0A–0C added shared domain repairs, a hosted web
API and a local web service. This document records how that work was audited,
which parts the desktop shares, how the optional web parts are kept separate,
and the exact controller contracts the desktop UI rebuild (Prompts 1–8) uses.

Short version: **one engine, one set of domain services, two optional HTTP
adapters.** The desktop calls controllers and services directly — never an
HTTP route, never a local server — and runs with no web package installed, no
backend, no cloud secrets and no network.

## 1. What 0A–0C changed, classified

Audited from the actual commit (`b68c795`) and code, not from earlier reports.

| Class | Modules | Status |
| --- | --- | --- |
| **(a) Shared domain/application** | `app/planning/workflow.py` (the one allocation → generation → save → freshness orchestration), `scope.py` (`OwnerScope`), `fixed_block_rules.py`, `errors.py` (`StaleInputsError`, `RegenerationRequiredError`, `ScopeError`), `application.py` (owner scoping, `reset_preview`/`reset_range`, fixed-block validation, timezone-aware eligibility), `allocation.py`, `preferences.py`, `time.py`, `csv_export.py`; `app/execution` (owner scoping, schema v6); `app/sync` (account operations, status, association preview/confirm) | Kept. Imports nothing from `backend/`, `app/web`, FastAPI, SQLAlchemy or Tk. |
| **(b) Desktop controllers/storage/sync** | `app/ui/planning_controller.py` (now delegates to `workflow`), `app/ui/app_services.py`, `app/ui/background.py`, `app/ui/schedule_page_controller.py`, SQLite repositories, `SyncService` | Kept; repaired and extended in this prompt (section 6). |
| **(c) Optional HTTP/browser/server adapters** | `backend/planning_api.py`, `backend/planning_repository.py`, `backend/browser_sessions.py`, `backend/preferences.py`, migration `0003_browser_sessions`, `app/web/` (`local_app.py`, `local_session.py`, `records.py`, `__main__.py`) | Kept, launched only through their own entry points. |

Findings that needed repair (all fixed, section 6): four operations the UI
prompts need existed only inside HTTP route bodies (preference views, the
allocation preview with its fingerprint, the CSV dry run, the engine
catalog); the desktop had no controller entry for incremental/no-op
generation, confirmed resets or CSV previews; the desktop read and generated
across every owner at once; nothing stopped the desktop and the local web
service from sharing one database's sync session; the local web service
cleared the device's active account on every start; shutdown could close
SQLite under a running transaction; and there was no desktop-only dependency
set to verify isolation with.

## 2. Module and dependency map

```text
Desktop (python -m app.app)                          Optional, separately launched
-------------------------------------------          --------------------------------------------
app/app.py  (CustomTkinter widgets)                   app/web/  (python -m app.web, loopback only)
   |                                                     local_app.py, records.py, local_session.py
   v                                                     |  uses backend.planning_api's router/DTOs
app/ui/*_controller.py, app_services.py,                 v
background.py  (Tk-free controllers;               backend/  (uvicorn --factory backend.app:create_app)
ControllerResult; worker registry)                    api.py, planning_api.py, planning_repository.py,
   |                                                  browser_sessions.py, sync.py, migrations/
   v                                                     |
   +-------------------+---------------------------------+
                       v
        Shared domain/application (no UI, no HTTP):
        app/planning/  workflow.py -> application.py (PlanningService) -> repository (SQLite)
                       allocation.py, service.py -> app/optimizer.py (one day engine;
                       Greedy Optimizer v1 unchanged), preferences.py, provenance.py,
                       occurrence.py, scope.py, fixed_block_rules.py, csv_*.py
        app/execution/ db.py (SQLite, migrations v1..v6), repository.py, service.py, instance_lock.py
        app/sync/      service.py (SyncService), engine.py, store.py, transport.py (stdlib urllib)
        app/productivity/
```

Dependency rules (enforced by tests, section 5):

- `app/planning`, `app/execution`, `app/sync`, `app/productivity`, `config`
  import nothing from `backend`, `app.web`, `app.ui`, FastAPI, Starlette,
  SQLAlchemy, uvicorn or Tk.
- `app/ui` and `app/app.py` import shared modules only — never `backend` or
  `app.web`. `app/ui/*` controllers may import Tk infrastructure
  (`app/ui/background.py`); server code must not import them.
- `backend/` and `app/web/` import shared modules; `app/web` reuses
  `backend.planning_api`'s router and DTOs (it is an HTTP adapter too).
- The desktop's cloud sync uses `app/sync/transport.py` (standard-library
  `urllib`) against the backend's bearer-token API; it never holds
  PostgreSQL credentials.

The backend's `ServerPlanningRepository` implements the same repository
interface over SQLAlchemy, so the hosted API runs the same `PlanningService`
and `workflow` code on PostgreSQL. There is no second engine or domain model.

## 3. Independent launch commands

| What | Install | Run |
| --- | --- | --- |
| Desktop app | `python -m pip install -r requirements-desktop.txt` | `python -m app.app` |
| CLI | same | `python -m app.main [--help]` |
| Full development/test setup | `python -m pip install -r requirements.txt` (desktop + pytest/ruff + backend) | `python -m pytest`, `python -m ruff check .`, `python -m compileall .` |
| Local web service (optional) | `requirements.txt` (needs FastAPI/uvicorn) | `python -m app.web --data-dir DIR --timezone ZONE [--port 8765] [--backend-url URL]` |
| Hosted backend (optional) | `python -m pip install -r requirements-backend.txt` | `DATABASE_URL=... JWT_SECRET=... python -m backend.migrate upgrade` then `uvicorn --factory backend.app:create_app --host 127.0.0.1 --port 8000` |
| PostgreSQL tests (optional) | disposable DB whose name contains `test` | `TEST_DATABASE_URL=postgresql://.../schedule_maxing_test python -m pytest -m postgres tests/backend` |

Desktop startup with `SCHEDULE_MAXING_BACKEND_URL` unset and no
`DATABASE_URL`/`JWT_SECRET`: no web or server module is imported, no socket is
bound or connected, no background thread keeps running (the sync loop is not
started), no cloud database is migrated and no browser session exists. With a backend URL configured, the
sync loop starts but stays inert until an account signs in; an invalid URL is
ignored with a warning.

## 4. Supported concurrency

- **One application process per database file.** The desktop app and the
  local web service each own a sync session on the database (the in-memory
  token, the selected account, the device's active account that new records
  are stamped with, the outbox, the background sync loop). Two such processes
  on one file could push the same outbox twice or switch the active account
  under each other, so each takes an OS lock on `<database>.lock`
  (`app/execution/instance_lock.py`: msvcrt on Windows, flock elsewhere)
  before opening the database. The second one is refused with
  `DatabaseInUseError` naming the holder; the desktop shows "already open in
  another Schedule Maxing process", the web launcher exits with code 1. The
  lock is released on close or when the process ends (no stale lock after a
  crash). **Simultaneous desktop + local web on the same database is not
  supported;** use them one after the other, or on different databases.
- The CLI (`python -m app.main`) takes no lock: it runs no sync session, and
  SQLite's own locking keeps its transactions safe next to a running app.
- Inside the desktop process: one connection shared by the Tk thread and
  background workers, serialized by the connection's re-entrant lock;
  `BEGIN IMMEDIATE` transactions; the sync loop never holds a transaction
  across a network call; generation re-reads and compares its whole input
  fingerprint inside the write transaction (`StaleInputsError` otherwise).
- The hosted backend is multi-user and multi-process by design (PostgreSQL
  row locks, documented in `docs/backend.md`); it never sees a device's
  unsynchronized changes.

## 5. Compatibility and verification results

The table below records the earlier Prompt 0 baseline, not the final UI build.
See [Milestone 4 completion](milestone-4-completion.md) for the new Prompt 6–8 checks.

Commands run for this prompt (Windows 10, Python 3.10.11):

| Check | Result |
| --- | --- |
| Baseline before changes: `python -m pytest` | 1222 passed, 1 skipped |
| Full suite after changes: `python -m pytest` | 1251 passed, 1 skipped (the PostgreSQL-only test without `TEST_DATABASE_URL`) |
| `python -m ruff check .` | All checks passed |
| `python -m compileall -q -x "[\\/]\.venv[\\/]" .` | exit 0 |
| Desktop-only venv (`requirements-desktop.txt` + pytest; FastAPI, Starlette, uvicorn, SQLAlchemy, Alembic, psycopg, PyJWT, argon2, httpx absent; it resolved customtkinter 6.0.0) running every suite except `tests/backend`, `tests/sync`, `tests/web` | 1050 passed, 0 skipped (display available, so the Tk widget and launch tests ran) |
| Isolation probes (`tests/test_desktop_isolation.py`: web packages made unimportable, all socket operations refused, cloud variables removed, temporary data directory) | imports clean; full create/edit/delete → generate → no-op → execute → CSV export/preview/import → reopen workflow; real `python -m app.app` launch and close — no web import, no socket, no leftover thread |
| Disposable PostgreSQL 18 cluster (temporary `initdb`, trust auth, 127.0.0.1) | `-m postgres tests/backend`: 6 passed; `tests/backend tests/sync tests/web` with `BACKEND_TESTS_ON_POSTGRES=1`: 204 passed; `python -m backend.migrate upgrade`/`check`: at head 0003 (browser-session migration preserved) |
| Real HTTP smoke: `uvicorn --factory backend.app:create_app` on that database | `/health` 200, register 201; the desktop's `SyncService` signed in, associated after preview, pushed over bearer-token HTTP; the server listed the task with its id |
| Real `python -m app.web` smoke | served on loopback, bootstrap session 200; the desktop was refused on its database while it ran and opened it after it stopped |
| CLI smoke with a temporary data directory | summary, legacy import + select-date generation + all three exports, identity-merging reimport of the canonical CSV, `--demo` |
| Copy of the repository-local legacy `data/executions.db` (SQLite backup from a read-only connection) | schema 1 → 6, execution ids preserved, all controllers work, reopen works, original byte-identical |

Data-compatibility tests (`tests/ui/test_data_compatibility.py`): a genuine
Milestone 3 (v5) database that was associated and synchronized — owned
project, tasks with tags/dependencies/recurrence, fixed block, generated
schedule with provenance, user and date preference layers, completed
execution, server shadow (server version 7), pending outbox operation,
cursor 42 — migrates to v6 and reopens with every row of every planning,
execution and sync table byte-identical, stays *current* (no regeneration
needed), and opens in its owner's workspace; a v1 database keeps its
execution history and work sessions.

Workflow through desktop callers (`tests/ui/test_desktop_generation_workflow.py`):
first generation, `already_current` with no row/version/timestamp/provenance/
sync change, incremental additions keeping ids and intervals, refusal with
`RegenerationRequiredError` then explicit full regeneration, history
protection of started work, fixed blocks, dependency order, the Tokyo
deadline, stale fingerprints and a concurrent write during generation (old
schedule kept), previewed/confirmed reset (defaults and history kept), CSV
preview without writes; the legacy `schedule_range` keeps its behavior and
the Greedy Optimizer v1 regression tests are unchanged.

Workspace/lifecycle (`tests/ui/test_desktop_workspace.py`,
`tests/sync/test_desktop_account_workspace.py`,
`tests/web/test_desktop_boundary.py`): ownerless default, no mixing of
owners in views/generation/export, the active account after a restart,
switching with dropped late results, sign-in claims nothing, preview writes
nothing, confirmed association, sync with the same ids, offline backend never
blocks local CRUD/scheduling, process exclusion both ways, no close under a
running transaction.

## 6. Repairs made in this prompt

- **Route-only logic moved below the routes:** `workflow.preview_allocation`,
  `workflow.preference_views`, `PlanningService.preview_record_batch` (the
  CSV dry run) and `preferences.ENGINE_DESCRIPTIONS`. The hosted and local
  routes now call them; responses are unchanged.
- **Desktop controller operations** (section 7) for generation modes, the
  no-op, freshness, allocation preview, preference views, engine catalog,
  confirmed reset and CSV preview. `ControllerResult` gained an optional
  `cause` (the structured error, e.g. `RegenerationRequiredError.problems`).
- **Explicit desktop workspace.** `SyncService.workspace_scope()` is the one
  rule for every client of a database: the account selected in this session,
  else the account active on the device (associated; the database stamps new
  records with it; it stays active across restarts until sign-out), else the
  ownerless workspace. `open_app_services` binds every controller to it; new
  tasks, blocks and executions created in an account workspace are that
  account's. The CLI stays device-wide (single-owner stores behave the same).
- **Switching** (`AppServices.switch_workspace`, `workspace_guard`,
  `run_in_background(still_current=...)`): new controllers per workspace, old
  ones keep their scope, stale results are dropped on the Tk thread.
- **Process exclusion** (`app/execution/instance_lock.py`) in the desktop and
  the local web service.
- **The local web service keeps the device's active account on start**
  (constructing its `SyncService` with the transport, like the desktop);
  `PUT /local/backend` still switches and signs out. Its reported workspace
  now matches the scope requests use.
- **Shutdown:** neither the desktop nor the local service closes SQLite while
  a transaction is still running; `close()` returns False and can be retried
  (process exit ends it). The desktop also retains SQLite while any background
  worker or sync loop is running, including workers awaiting network responses
  outside a transaction. After the initial timeout, the window hides and retries
  until safe to close. Tk cyclic collection runs on the desktop thread, and its
  variables/fonts are finalized there when closing. Workers never call Tk; results are delivered by a
  Tk-thread poll and never after shutdown or to a destroyed widget.
- **Dependencies:** `requirements-desktop.txt` (desktop runtime only, incl.
  `tzdata` for Windows time zones); `requirements.txt` includes it plus the
  test tools and the backend. CI gained a `desktop-only` job.

## 7. Controller contracts for the desktop UI prompts

Every method returns `ControllerResult[T]` (`ok`, `value`, `error`, `cause`)
and is safe to call from `run_in_background` workers; widgets never call
services, repositories, SQL, HTTP or the sync engine directly.

**Application lifetime (`app/ui/app_services.py`)**

```python
services = open_app_services(db_path=None, *, timezone=None, project_root=None, backend_url=None)
services.planning_controller / .execution_controller / .productivity_controller / .sync_service
services.workspace            # Workspace(scope: OwnerScope, epoch: int)
services.switch_workspace(scope=None) -> Workspace   # after sign-in/out/association; re-read views afterwards
services.workspace_guard() -> Callable[[], bool]     # pass as run_in_background(still_current=...)
services.close(timeout=10.0) -> bool
describe_startup_failure(error, db_path) -> str      # incl. DatabaseInUseError
run_in_background(widget, work, on_done, *, registry=None, still_current=None) -> bool
# Without still_current, the registry's result_guard (AppServices.workspace_guard, installed by
# open_app_services) is used: a result never reaches the UI after a workspace switch.
```

The shell (Prompt 1, [desktop-layout.md](desktop-layout.md)) hosts pages that may implement
`on_show()`, `set_layout(LayoutMode)` and `on_appearance_changed()`; reusable widgets are in
`app/ui/components.py`, design tokens in `app/ui/theme.py`, appearance persistence in
`app/ui/ui_settings.py`.

**Planning (`PlanningController`)** — records are fresh snapshots; updates
and deletes pass the version read (`expected_version`); creates in an account
workspace are stamped with its owner.

```python
# tasks / blocks
add_or_update_task(task, *, expected_version=None) -> Task
add_or_update_tasks(tasks, *, expected_versions=None) -> list[Task]
remove_task(task_id, *, expected_version) -> None
get_task(task_id) / get_tasks(ids) / list_tasks()
save_fixed_block(block, *, expected_version=None) -> FixedBlock     # validated (window, overlap) before writing
delete_fixed_block(block_id, *, expected_version) -> bool
get_fixed_blocks(day) / set_fixed_blocks(day, blocks, *, expected_versions)
load_range(start, end, *, scope=RangeScope.ELIGIBLE) -> PlanningRange
get_placements(day) -> list[ScheduledTask]
# generation (the shared workflow; Prompt 4)
preview_allocation(start, end, *, scope=PLANNED) -> workflow.AllocationPreview   # .allocation, .fingerprint, .freshness
generate(start, end, *, generate_start=None, generate_end=None, scope=PLANNED,
         mode=GenerationMode.FULL, protect_history=True, expected_fingerprint=None,
         preserve_on_empty=False) -> workflow.GenerationOutcome     # "nothing_placed": empty run, nothing written
    # outcome.status "generated" | "already_current"; .outputs[day].unscheduled; .kept_ids; .allocation.unallocated
    # failures: cause StaleInputsError | RegenerationRequiredError(.problems) | MandatoryTaskSchedulingError(.failures)
day_freshness(dates) -> dict[date, workflow.DayFreshness]   # CURRENT/STALE(+StaleReason)/NONE, record, placements
day_state(day) / day_states(dates) -> SelectedDayState      # legacy view of the same freshness
schedule_range(start, end, *, scope=PLANNED) / allocate_range / generate_day   # legacy Make Schedule, unchanged
# preferences (Prompts 4, 7)
engine_descriptions() -> dict[OptimizerMode, str]           # labels: Normal=precise_greedy, ADHD friendly=adhd_friendly
preference_views(start, end) -> workflow.PreferenceViews    # per day: effective, inherited, date_layer (with version)
resolve_preferences(day) -> DayPreferences
user_preferences() / date_preferences(day) -> PreferenceRecord | None
set_user_overrides(overrides | None, *, expected_version=None)
set_date_overrides(day, overrides | None, *, expected_version=None)
set_engine_mode(mode)                                        # user default, one transaction
update_date_overrides(day, change, *, expected_version)      # read-compare-modify one date layer (Prompt 4)
active_placement_dates(task_ids) -> dict[id, list[date]]     # where tasks are already scheduled
# reset and CSV (Prompts 4, 5)
reset_preview(start, end) -> ResetPreview                   # counts, ids, cascades, blocked, token
reset_range(start, end, *, confirmation=token) -> ResetResult
clear_range(start, end, *, include_planning_data)            # the older reset scopes, unchanged
preview_csv_file(path, *, allow_updates=False) -> BatchApplyResult   # canonical v2 only, writes nothing
import_csv_file(path, *, anchor_date, mode, allow_updates=False)
export_planning_csv(path, *, start_date=None, end_date=None, include_deleted=False)
```

Day workspace (Prompt 4, implemented; [desktop-day.md](desktop-day.md)): `DayScheduleController`
(`app/ui/day_controller.py`, a one-date `SchedulePageController`) adds `load() -> DaySnapshot`,
`engine_options()`, `set_engine(mode | None, expected_version=)`, `make_schedule_for(day)` /
`regenerate_for(day) -> DayRun`, `preferences()`, `save_preference/inherit_preference/clear_preference/
reset_date_preferences(..., expected_version=)`, `reset_plan()` / `reset_day(plan)` and
`csv_plan(path)` / `apply_csv(plan, legacy_mode=)`; engine labels are
`app.planning.preferences.ENGINE_LABELS`.

Week and Month (Prompt 5, implemented; [desktop-calendar.md](desktop-calendar.md)): `CalendarController`
(`app/ui/calendar_controller.py`) with `period` / `select(day)` / `shift(delta)` / `go_today()` /
`go_to_month(year, month)` / `month_choices()`, `load_for(period) -> CalendarSnapshot` (safe in a
worker), `reset_plan()` / `reset_period(plan)`; calendar arithmetic in `app/ui/calendar_model.py`.

Task form (Prompt 3, implemented; [desktop-task-form.md](desktop-task-form.md)): pages use
`SchedulePageController.blank_draft()`, `editor_options(editing, category=)`, `draft_for(ref)`,
`save_draft(draft, editing=ref)` (cause `FormErrors` with field -> message on a refusal) and
`delete_description(ref)`; `PlanningController.list_projects()` feeds the project choice.

Projects and Allocation (Prompt 6): native `ProjectsPage` / `AllocationPage` use
`ProjectsController` / `AllocationController` through `PlanningController`.
Project CRUD, version checks and reference-safe deletion use the existing service.
`preview_allocation` and `inputs_fingerprint` support date-only previews; selected-day
generation retains the preview range and checks its fingerprint. Calendar/project
filters affect presentation only. See [desktop-projects-allocation.md](desktop-projects-allocation.md).
There is no archive or recurrence expansion.

Settings (Prompt 7): `SettingsController.load/change/set_engine/reset` edits the
versioned user preference layer through `PlanningController`. `SettingsPage` reuses
`PreferencesEditor`; date overrides are never changed by default edits. Settings pages
are rebuilt on workspace changes. See [desktop-settings.md](desktop-settings.md).

**Execution/productivity:** `ExecutionController` (`get_or_create_canonical_execution`,
`start/pause/resume/complete/skip/cancel`, `find_execution_for_placement`,
`record_feedback`, `list_executions`, `reset_all_history` — scoped to the
workspace) and `ProductivityController` (`build_dashboard`,
`predict_duration`, `export_execution_history`, `reset_all_history`).

**Accounts and sync (Prompt 2, implemented; [desktop-accounts.md](desktop-accounts.md)):** widgets use
`AccountController` (`app/ui/account_controller.py`: `connection()`, `configure_backend(url)`,
`check_backend()`, `register()`, `sign_in()`, `sign_out()`, `profile()`, `association_preview()`,
`associate(token)`, `sync_now()`, `conflicts()`, `resolve(id, choice)`, each a `ControllerResult`, plus the
`ConnectionView`/`ConflictView` view models), which delegates to `SyncService` — `configured`, `account`,
`signed_in`, `workspace_scope()`, `workspace_account()`, `set_transport()`,
`register()`, `sign_in()`, `sign_out()`, `profile()`, `update_profile()`,
`check_connectivity()`, `association_preview()` (counts, problems, token),
`associate_local_data(confirmation=token)`, `status()` (reachability, signed
in, auth required, in progress, pending, conflicts, last successful sync,
last error), `sync_now()`, `wake()`, `list_conflicts()`, `get_conflict()`,
`conflict_actions()`, `resolve_conflict(id, "accept_remote"|"keep_local")`.
Network calls belong in background workers; after sign-in, sign-out or
association call `services.switch_workspace()`. Signing in never claims
ownerless records.

## 8. Remaining limitations

- Simultaneous desktop and local-web use of one database is refused, not
  supported. Two desktop windows on one database are refused the same way.
- A record of another owner, or ownerless records left while an account is
  active, is hidden from the current workspace rather than listed; the
  desktop has no view of "other workspaces" yet (Prompt 2's account UI shows
  counts through the association preview).
- The CLI remains device-wide.
- `requirements-desktop.txt` has lower bounds only: a fresh install resolved
  customtkinter 6.0.0 (the development venv has 5.2.2); both pass the suites
  here, including the widget tests.
- CI's main and desktop-only jobs now run under Xvfb so native widget tests execute.
  Without a display outside those jobs, the tests still report explicit skips.
- The local web service can still stop mid-request if killed; on Windows a
  venv launcher's child may outlive a killed launcher briefly — the lock is
  released when that process ends.
