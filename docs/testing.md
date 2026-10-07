# Testing: suites, tiers and the 2026-10 speed-up

This page explains how to run the tests, how they are grouped, and what was
changed in October 2026 to make the suite faster. No test was removed, skipped
or weakened by that work, and no application code changed -- only test
infrastructure.

The sizes, timings and failure reports below are historical measurements from
the test-speed work, not current suite totals or a current verification result.
Use `python -m pytest --collect-only -q` to inspect current collection.

## Commands

| Purpose | Command | Size | Time (Windows 10, Python 3.10) |
| --- | --- | --- | --- |
| Development suite (default while implementing) | `python -m pytest -m dev` | 1,600 tests | about 1 m 47 s |
| Complete suite (milestones, final verification) | `python -m pytest` | 1,911 tests | about 12 m 46 s |
| One file or test while debugging | `python -m pytest tests/path/test_x.py -k name` | -- | seconds |

Other selections: `-m ui`, `-m system`, `-m slow`, `-m integration`, `-m unit`.
CI still runs the complete suite (`xvfb-run -a python -m pytest`).

The intended workflow (also in `CLAUDE.md` and `AGENTS.md`): run focused tests
while implementing, run the development suite once before calling a batch of
work complete, and run the complete suite (with `python -m compileall .` and
`ruff check .`) only at the end of a milestone, after major cross-cutting
changes, on request, or before merge/release.

## Tiers

Markers are assigned automatically at collection by `tests/conftest.py` using
the rules in `tests/test_tiers.py`; they are registered in `pyproject.toml`, so
there are no unknown-marker warnings.

| Marker | Tests | Meaning |
| --- | --- | --- |
| `unit` | 1,401 | In-process domain and controller tests: no server, no window, no subprocess |
| `integration` | 510 | Server-backed, subprocess or real-window tests |
| `system` | 234 | Multi-system suites: desktop + server sync (`tests/sync`), the local web profile (`tests/web`), direct PostgreSQL mode (`tests/direct`) |
| `ui` | 52 | Tests that open real Tk/CustomTkinter windows |
| `slow` | 95 | `ui` tests, subprocess probes, Alembic migration-path suites, and individual tests measured at about a second or more |
| `dev` | 1,600 | The development suite: not `slow`, not `ui`, not `system` |

- A module is `ui` when its source builds the desktop app or a Tk root
  (`WINDOW_PATTERN` in `tests/test_tiers.py`), so a new real-window module is
  classified without editing anything.
- To keep a new slow test out of the development suite, decorate it with
  `@pytest.mark.slow` or add it to `SLOW_TESTS` in `tests/test_tiers.py`.
- The development suite covers the optimizer and scheduling modes,
  constraints/PERT/reward, planning, execution tracking, recurrence,
  productivity/ML, window-free UI controllers and models, the CLI and import
  tests, and the fast backend API tests. It leaves out real-window tests, the
  sync/web/direct suites, subprocess isolation probes, migration-path suites and
  the listed slow tests -- all of which still run in the complete suite.

## What the profile showed (before)

One full run on 2026-10-02: 1,910 tests, 1,063 s (17 m 43 s).

- About half the time (516 s, 249 tests) was real-window desktop tests.
- Fixture setup was 355 s (34%), almost all in the server-backed suites: every
  backend, sync and direct-mode test replayed all 12 Alembic migrations
  (about 0.42 s each), and no fixture used a session or module scope.
- Registrations and sign-ins each paid about 0.05 s of Argon2 hashing; the sync
  test helper signed in again for every request.
- The web suite ran `gc.collect()` before every test.
- About 1,170 pure domain tests ran in about 56 s combined.

## Changes made

| Change | Where | Why it is safe |
| --- | --- | --- |
| **Migrated database template.** One database is migrated per test run with the real Alembic chain; each test gets a private copy through SQLite's backup API | `tests/db_template.py`; the `engine`/`server` fixtures in `tests/backend`, `tests/sync`, `tests/direct` | Every test still has its own isolated database. Tests about migrations still call the real upgrade/downgrade. PostgreSQL runs (`BACKEND_TESTS_ON_POSTGRES=1`) still migrate each schema for real |
| **Cheap password hashing in tests.** Argon2id with minimal cost parameters | autouse fixture in `tests/conftest.py` | Production settings in `backend/passwords.py` are untouched; hashing and verification are still real. Subprocess tests use the production hasher, and a test that checks production parameters opts out with `@pytest.mark.real_password_hashing`. The fixture never imports server packages into a desktop-only run |
| **Token reuse in the sync test helper** | `Server.headers` / `Server.get` in `tests/sync/conftest.py` | A token that was ended (for example by a password reset) triggers one fresh sign-in |
| **Condition-based UI waiting.** `settle()` returns once the app has been unchanged for 60 ms (no worker, no pending layout check, same geometry, layout and focus), with the old duration as a cap | `tests/ui/test_desktop_shell.py` | The cap prevents hangs. The resize test that proves no late event arrives keeps a deliberate fixed wait (`wait_fixed`) |
| **Tk lifecycle cleanup.** A closed app's leftover timers are cancelled, and destroyed windows are removed from CustomTkinter's global registries after each test | `tests/tk_cleanup.py`, the close helpers, an autouse fixture in `tests/conftest.py` | Only destroyed windows are touched; a close deferred while workers finish is left alone |
| **Web suite garbage collection once per module** instead of before every test | `tests/web/conftest.py` | No web test creates Tk objects, so new Tk garbage can only come from earlier modules |
| **Automatic tier markers and the development suite** | `tests/test_tiers.py`, `tests/conftest.py`, `pyproject.toml` | Selection only; the complete suite is unchanged |

## Results

| | Before | After | Change |
| --- | --- | --- | --- |
| Complete suite | 1,063 s (17 m 43 s) | 766 s (12 m 46 s) | −297 s (−27.9%) |
| Fixture setup, all tests | 355 s | 79.5 s | −78% |
| `tests/backend` | 195.6 s | 67.9 s | −65% |
| `tests/direct` | 124.1 s | 68.0 s | −45% |
| `tests/web` | 71.5 s | 14.8 s | −79% |
| `tests/sync` | 164.3 s | 126.8 s | −23% |
| `tests/ui` | 439.9 s | 419.5 s | −5% |
| Development suite | did not exist | 1 m 47 s (1,600 tests) | -- |

The complete-suite run after the changes: 1,909 passed, 1 failed (the Tk flake
below), 2 skipped (the PostgreSQL module without `TEST_DATABASE_URL`, and a
symbolic-link test this Windows account cannot run).

## What stays slow, and why

- **Real-window tests (about 420 s).** Building the full app takes about 4.6 s
  and closing it about 1.2 s; that is CustomTkinter drawing every page's widgets
  in the application itself, not test waiting. Tests that verify persistence
  open the app several times on purpose.
- **The desktop cloud workflow test** (`tests/sync/test_desktop_milestone4.py`),
  about 45 s on its own.
- **Subprocess probes** (dependency isolation, launching the app in a child
  process, migration and config command lines), about 30 s.
- **Collection**, about 8.5 s per run, from importing every test module.

## Known flaky test

`TclError: invalid command name "tcl_findLibrary"` appears intermittently in one
real-window test per run (a different test each time). It is raised inside
`_tkinter.create`, while a new Tk interpreter is being created. The lifecycle
cleanup above removed the leaked windows and stale timers but did **not** fix
this; its cause is not yet known. The affected test passes when rerun on its
own.

## Deliberately not done

| Idea | Reason |
| --- | --- |
| One shared app window per test module | Resetting state between tests could not be shown to be safe, and restart tests must stay real restarts |
| A template for the desktop's local SQLite files | Tests for legacy-data adoption and fresh installs depend on opening a database file that does not exist yet |
| Running tests in parallel (pytest-xdist) | It would change the suite's normal configuration, and Tk tests need one main thread |
| Fewer restarts, merged end-to-end scenarios, or dropping subprocess probes | Each would lose real coverage |

## Not verified

The PostgreSQL test runs (`python -m pytest -m postgres tests/backend`, and the
backend/sync/direct suites with `BACKEND_TESTS_ON_POSTGRES=1`) were not run for
this work: no disposable `TEST_DATABASE_URL` was available. Their fixtures were
left on the real per-schema migration path.
