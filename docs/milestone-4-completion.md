# Milestone 4 desktop completion

Verification date: 2026-09-24. This report covers the continuation of Prompts
6–8, including regression checks of the existing Prompts 0–5. Earlier 0A–0C
results are not evidence for this final implementation.

## Work completed

The initial audit found working Day, Week, Month, task forms and account/sync
screens. Prompt 6 had project/allocation controllers but placeholder pages.
Settings exposed appearance and an obsolete runtime reward editor. Prompts were
finished sequentially: Projects/Allocation, then Settings/account details/About,
then integration and regression verification.

- **Prompt 6:** native project CRUD, reference-safe deletion, confirmed bulk
  task reassignment, saved work/date links and calendar project filters. Allocation
  supports week/month previews, capacity and unallocated reasons, explicit
  recalculation, and generation of only the selected day using the preview's
  range and freshness fingerprint. Filters change presentation, not engine input.
- **Prompt 7:** shared default/date preference editor, version-aware persistence,
  inheritance/reset and explicit null semantics. Defaults preserve date overrides.
  Normal and ADHD friendly use the existing engine catalog. Related-tag editing
  explains first-tag scoring; unused optimizer controls are hidden. English,
  appearance and scale persist locally. Account shows the real profile with a
  masked email and centralized Normal plan fallback. About credits Ramtin Rezaei.
- **Prompt 8:** realistic native/two-device integration coverage, improved narrow
  Projects/Allocation layouts, readable project choices, safe worker shutdown and
  error delivery. SQLite and its process lock remain open until workers finish.
  Tk resource cleanup and cyclic collection run on the desktop thread, preventing
  account-switch cleanup from reaching Tcl on an HTTP worker. CI's main and
  desktop-only jobs use Xvfb to run widget tests.

The greedy baseline was not changed. Desktop widgets still call shared Python
services directly. Optional HTTP adapters, bearer sync and browser-session
migrations remain independent; no web frontend or deployment was added.

## Acceptance evidence

| Area | Result and evidence |
| --- | --- |
| Native offline startup and dependency isolation | Subprocess launch probes block sockets/server imports; a separate clean desktop-only environment has all nine optional server packages absent. |
| Day, task form, time precision, engine choice | Native widget/controller suites cover actual callbacks, minute inputs, validation, Normal/ADHD choices, no-op and incremental generation, stale state and explicit regeneration. |
| Week and Month | Native and headless tests cover Open Day/Back, true 28/29/30/31-day months, year boundaries, resets, categories, ordering and display-only filtering. |
| Projects and Allocation | New controller/widget tests cover CRUD, bulk move, reference-safe refusal, owner isolation, dependencies across projects, reasons, stale previews, selected-day generation and reopen. |
| Settings and profile | New controller/widget tests cover layered defaults/date overrides, clear versus inherit, reset, conflict/version handling, engine relevance, real masked profile and appearance persistence. |
| Offline → account → cloud → second device | `tests/sync/test_desktop_milestone4.py` drives the native app with disposable real services: fixed/flexible work and project, registration/login, cancel/confirm association, generation, edit/regenerate, execution, CSV, sync, real conflict, restart and logout isolation. Repeated on PostgreSQL. |
| Persistence, atomicity and history | Planning/CSV/sync suites cover invalid writes, rollback, stale-input races, tombstones, ownership, format v2, protected execution and recurring placements remaining distinct. |
| Shutdown and concurrency | Worker/service tests cover in-flight work, delayed writes, repeated close and unexpected worker failures; process tests enforce one desktop/local-web process per database. |
| Retained server/web components | Backend, sync and optional-web suites run independently; disposable PostgreSQL migration upgrade/check reaches head `0003`. |
| CLI | Help and demo completed with temporary legacy CSV, exact JSON and canonical planning CSV outputs. |
| Visual review | Native light Settings at 1200×850 and dark Settings/Projects/Allocation at 700×700 inspected. Automated widget tests also exercise sizing, themes and navigation. |

## Reproduce verification

| Check | Actual result |
| --- | --- |
| Final full suite, live Tk and PostgreSQL available | **1,384 passed, zero skips**, 3 dependency warnings; 398.90 s |
| Clean desktop-only subset (CustomTkinter 6.0.0) | 56 passed, no skips or warnings; 39.88 s |
| PostgreSQL backend/sync/optional web, excluding native account modules | 213 passed, 3 dependency warnings; 136.21 s |
| Native account and milestone integration on PostgreSQL | 4 passed, 1 dependency warning; 78.25 s |
| PostgreSQL migrations | Upgrade successful; current and head both `0003` |
| Ruff | All checks passed |
| Source compilation | `app`, `backend`, `config`, `tests`: exit 0 |
| CLI help/demo | Exit 0; demo imported 3 tasks/4 fixed blocks and saved 3 placements |

Use Python 3.10 or newer; this workstation was verified with Python 3.12 on
Windows and a live Tk display. Install `requirements.txt` for the full suite.
For the independent desktop check, create a different virtual environment and
install only `requirements-desktop.txt` and `pytest`.

```powershell
python -m pytest -q -o faulthandler_timeout=90
python -m ruff check .
python -m compileall -q app backend config tests

# Fresh desktop-only environment:
python -m pytest tests/test_desktop_isolation.py tests/ui/test_settings_controller.py tests/ui/test_settings_page.py tests/ui/test_projects_allocation.py tests/ui/test_planning_pages.py tests/ui/test_app_services.py tests/ui/test_calendar_controller.py tests/test_main_cli.py -q -o faulthandler_timeout=90

# Disposable PostgreSQL database only; never use production data:
$env:TEST_DATABASE_URL = 'postgresql://sm_test@127.0.0.1:55439/schedule_maxing_completion_test'
$env:DATABASE_URL = $env:TEST_DATABASE_URL
python -m backend.migrate upgrade
python -m backend.migrate check
$env:BACKEND_TESTS_ON_POSTGRES = '1'
python -m pytest tests/backend tests/sync tests/web --ignore=tests/sync/test_desktop_account_page.py --ignore=tests/sync/test_desktop_milestone4.py -q
python -m pytest tests/sync/test_desktop_account_page.py tests/sync/test_desktop_milestone4.py -q
```

The PostgreSQL runs were split to avoid competing native test windows. They use
private schemas and temporary device databases. The local cluster was PostgreSQL
18 on loopback; no existing user database or production service was used.

The first full integration run reported 1,382 passes and two test failures. One
asserted that the replaced legacy reward page still existed; it now checks the
actual Settings-based page inventory. The calendar creation-order fixture could
give tasks identical Windows clock timestamps and then encounter the documented
UUID tie-breaker. It now supplies distinct creation instants while retaining the
strict expected order. Neither fix changes engine behavior or relaxes assertions.
The final full rerun passed every test.

## Limitations and environment notes

- Performance on Account is the explicitly allowed future-milestone empty state;
  existing Productivity and Execute remain available. No recurrence expansion,
  project archive, billing, additional language or web frontend is claimed.
- Overnight/ambiguous DST inputs are refused; the planning timezone is configured
  at startup, not silently changed by a Settings edit. CSV v2 is planning exchange,
  not a complete application backup. Back up SQLite after closing the app.
- Automated native tests and screenshot inspection are evidence for this Windows
  display. A complete manual multi-monitor/DPI, screen-reader and physical keyboard
  acceptance pass was not performed. Linux CI changes have not been run remotely.
- The repository's pre-existing `.venv` points to a removed Python 3.10 install.
  Verification used independent Python 3.12 environments; that old environment was
  not overwritten. Create a fresh environment using a working interpreter, install
  `requirements-desktop.txt`, and run `python -m app.app`; no Node build is needed.
- Three full-suite warnings come from FastAPI/Starlette's deprecated TestClient
  HTTPX/cookie interfaces. They are dependency warnings, not skipped or suppressed
  checks. Dependencies were not upgraded merely to hide warnings.

See [desktop/web boundaries](desktop-web-boundaries.md),
[Projects and Allocation](desktop-projects-allocation.md),
[Settings](desktop-settings.md), and [accounts](desktop-accounts.md) for behavior
and installation/configuration details.
