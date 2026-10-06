# Prompt 3 — PyInstaller production build

Implement only this prompt, then stop with a completion report. Prerequisites: Prompts 1 and 2. Read `CLAUDE.md`,
`docs/milestone-9-prompts/README.md` and `00-audit-and-architecture.md` (sections 1.3, 1.4, 2, 3), inspect `git status`
and the code below before editing. Do not commit, push or read `.env`.

Objective: a reproducible `onedir`, windowed, 64-bit PyInstaller build at `dist/ScheduleMaxing/ScheduleMaxing.exe` that
runs on a Windows computer with no Python, built by one script, and proven by an automated smoke test of the packaged
executable. A build that merely completes is not success.

Audit findings addressed: no packaging exists (1.2); a build from the full development environment would bundle the
server and its database drivers (1.3); the checkout `.env` must never be bundled (1.3); package data and hidden imports
that static analysis misses (1.4); no icon (1.2).

Inspect first: `app/desktop.py`, `app/runtime.py`, `app/version.py`, `requirements-desktop.txt`, `requirements.txt`,
`app/ui/direct_services.py` and `app/persistence/__init__.py` (the lazy optional imports), `app/sync/credentials.py`
(keyring), `app/productivity/ml_model.py` / `ml_persistence.py` (pandas, scikit-learn, joblib),
`app/planning/system_clock.py` (zoneinfo/tzdata), `tests/isolation_probe.py` (an existing end-to-end headless workflow
worth reusing), `.gitignore`.

Required work:

1. `requirements-build.txt`: `-r requirements-desktop.txt` plus a pinned-minimum PyInstaller (and Pillow only if the
   icon script needs it). Do not add PyInstaller to `requirements-desktop.txt`.
2. Icon: `packaging/windows/assets/ScheduleMaxing.ico` containing 16, 24, 32, 48, 64, 128 and 256 px images. No artwork
   exists, so generate a simple neutral placeholder with a small committed script
   (`packaging/windows/make_icon.py`) and commit the resulting `.ico`. State in the report that it is a placeholder.
   Set it as the window icon in `app/app.py` through `runtime.resource_path(...)`, tolerating a missing file.
3. `packaging/windows/version_info.py`: writes the PyInstaller version-resource file from `app/version.py`
   (FileVersion, ProductVersion, ProductName "Schedule Maxing", FileDescription, InternalName, OriginalFilename
   `ScheduleMaxing.exe`, CompanyName from a single neutral constant — no personal name or e-mail).
4. `packaging/windows/ScheduleMaxing.spec` (committed, readable, commented):
   - script `app/desktop.py`; name `ScheduleMaxing`; `console=False`; `onedir`; icon and version resource; no UPX;
   - `datas`: `config/task_preference.yaml` → `config`, the icon → `assets`, and the package data of `customtkinter`
     and `tzdata` (use `collect_data_files`). Nothing else from the repository root;
   - hidden imports and submodules needed by `keyring` (Windows backend, `win32ctypes`), scikit-learn/scipy, pandas and
     pydantic — add only what the smoke test proves necessary, and comment why each is there;
   - `excludes`: `backend`, `app.web`, `app.persistence.direct`, `app.persistence.executions`,
     `app.persistence.planning`, `app.persistence.verify_render`, `sqlalchemy`, `psycopg`, `psycopg_binary`, `alembic`,
     `fastapi`, `starlette`, `uvicorn`, `httpx`, `jwt`, `argon2`, `dotenv`, `pytest`, `ruff`, `tests`, `benchmarks`,
     `matplotlib`, `IPython`, `tkinter.test`.
   Paths in the spec are derived from the spec's own location, never from the working directory.
5. `packaging/windows/build_windows.ps1` (PowerShell 5.1 compatible; stops on the first error):
   - parameters for skipping tests, skipping the installer step (Prompt 4 fills that step in) and a clean rebuild;
   - creates or reuses a dedicated build virtual environment (for example `.venv-build`, git-ignored) from
     `requirements-build.txt`, so server packages are absent by construction;
   - reads the version from `app/version.py`, generates the version resource, runs PyInstaller with the spec,
     runs the bundle checks and the smoke test below, and prints the output path and size;
   - fails if the version cannot be read or any step fails.
6. Bundle checks (script, run by the build and by the test below): the output contains no `.env*`, no `backend`,
   `sqlalchemy`, `psycopg`, `fastapi`, `uvicorn` or `alembic`, no `tests`, and no file matching a secret pattern
   (`DATABASE_URL=`, `JWT_SECRET=`); it does contain the YAML template, the CustomTkinter assets, tzdata and the icon.
7. Packaged smoke test, `packaging/windows/smoke_test.py`, driven through a hidden self-test switch on the entry point
   (for example `ScheduleMaxing.exe --self-test <report.json>`), implemented in `app/desktop.py` with the logic in a
   small Tk-free module so it is unit-testable:
   - runs with `SCHEDULE_MAXING_DATA_DIR` set to a temporary folder and the backend switched off;
   - reports: frozen flag, version, resource root, that the YAML template loaded from the bundle, that the SQLite
     database was created in the data directory at the latest schema version, that a task can be created, a day
     generated with each scheduling mode, an execution recorded, a planning CSV exported and re-imported, that
     `customtkinter` themes, `tzdata` (`ZoneInfo("America/Toronto")`), `keyring`'s backend, `pandas`, `sklearn` and
     `joblib` import and work, and that a real window can be created and destroyed;
   - a second run against the same folder proves the data survived the restart;
   - exits non-zero with a readable report on any failure. The switch must not exist as a user-visible feature and must
     never touch the real data directory.
   The script also runs the executable from a copy in a path containing spaces.
8. `.gitignore`: `build/`, `dist/`, `.venv-build/`, generated version-resource file, installer output folder.
9. `docs/windows-distribution.md`: prerequisites, the one build command, what the build excludes and why, how to run
   the smoke test, expected size, known limitations.

Constraints: no changes to business logic to make packaging work — fix packaging in the spec; if a code change is
truly needed, keep it inside `app/runtime.py` / `app/desktop.py` and explain it. No `onefile`. No secrets in the spec.

Tests to add (`dev` tier, no build required): `tests/test_packaging_config.py` — the spec file names the entry script,
windowed mode, the icon and every required exclude; the icon and YAML it references exist; the version-resource
generator produces the version from `app/version.py`; the self-test logic passes against a temporary data directory
from source. Mark any test that runs a real build `@pytest.mark.slow` and skip it when PyInstaller is absent.

Verification — actually run it: `packaging/windows/build_windows.ps1`, the bundle checks and the smoke test on this
machine. Then once `python -m pytest -m dev` and `python -m ruff check .`. Report the bundle size and start-up time.
Running on this machine does not prove a clean machine works: list that as manual verification for Prompt 7.

Completion report: files changed; exact build command; hidden imports/data added and why; bundle size; smoke-test
output; what remains manual (clean Windows 10 and 11 machines, antivirus/SmartScreen behaviour); commands the owner
must run; tools the owner must install.
