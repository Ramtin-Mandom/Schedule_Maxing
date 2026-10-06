# Milestone 9 — Windows distribution: audit and architecture

Audited on 2026-10-05, branch `SMV2-M9` at `396eb0d`, clean tree, Python 3.10.11 (`.venv`), Windows 10 Home 19045.
This audit changed no code. Line numbers refer to that revision; the checkout wins where they have drifted.

Baseline checks run for this audit:

| Check | Result |
|---|---|
| `python -m ruff check .` | passed |
| `python -m compileall app config -q` | passed (exit 0) |
| `python -m pytest -m dev` | 1730 passed, 2 skipped, 350 deselected, 4 warnings (139 s) |

Not run: the full suite, the PostgreSQL suites, any packaged build (PyInstaller and Inno Setup are not installed here).

---

## 1. Current-state audit

### 1.1 What already supports distribution

- **Per-user data directory.** `config/settings.py:90-118` (`default_user_data_dir`) resolves `%LOCALAPPDATA%\ScheduleMaxing`
  on Windows, independent of the working directory and of the checkout. `resolve_data_dir` (`:130-136`) honours the
  `SCHEDULE_MAXING_DATA_DIR` override. Nothing writable defaults to the source tree.
- **SQLite location, creation and migration.** `app/execution/db.py:1493-1529` (`get_connection`) creates the directory,
  opens `executions.db`, enables foreign keys and runs `initialize_schema` (`:1314-1377`). Schema versioning uses
  `PRAGMA user_version` with an ordered `MIGRATIONS` tuple; each migration is atomic with its version bump
  (`:1358-1367`, and the self-managed `_migrate_v1_to_v2`, `_migrate_v3_to_v4`, `_migrate_v11_to_v12`),
  `foreign_key_check` runs before commit and `quick_check` after. A database newer than the code is refused untouched
  (`:1340-1344`), so a downgrade cannot damage data.
- **Other local state lives beside the database.** `ui_settings.json` (`app/ui/ui_settings.py:50-52`),
  `task_defaults.json` (`app/ui/task_defaults.py:131`), the instance lock `executions.db.lock`
  (`app/execution/instance_lock.py:52-54`), and the ML artifact pair (`app/productivity/ml_persistence.py:52-57`).
  Settings files are written atomically (`ui_settings.py:82-84`, `task_defaults.py:185-187`).
- **Startup failure is shown, not swallowed.** `ScheduleOptimizerApp.__init__` (`app/app.py:277-290`) catches a storage
  failure and shows `describe_startup_failure` (`app/ui/app_services.py:299-315`) in a window and a message box.
- **Offline-first.** `open_app_services` makes no network call. `SyncService.start()` runs `sync_now()` on a daemon
  thread (`app/sync/service.py:828-845`); session restore happens inside that thread (`:642`). `HttpTransport` uses a
  10 s timeout and maps every network failure to `TransportError` (`app/sync/transport.py:196-222`). Failures back off
  exponentially (`service.py:819-822`). `tests/test_desktop_isolation.py` proves a full workflow with every socket blocked.
- **No secrets in the client.** The desktop reaches the backend only over HTTPS at a public address
  (`config/settings.py:179`, `DEFAULT_BACKEND_URL`). The refresh credential is kept in Windows Credential Manager through
  `keyring` (`app/sync/credentials.py`), never in the database or a file. Local storage reads no `.env`
  (`app/app.py:665-666`).
- **Desktop dependency boundary.** `requirements-desktop.txt` is a desktop-only profile, and CI's `desktop-only` job
  (`.github/workflows/ci.yml:35-62`) asserts the web/server packages are absent.
- **Windows time zone.** `app/planning/system_clock.py:195-207` reads the zone from the registry; `tzdata` supplies the
  IANA database.
- **Single instance.** `acquire_instance_lock` (`app/ui/app_services.py:253`) refuses a second process on the same database.

### 1.2 What is incomplete

- **No packaging at all.** No `.spec`, `.iss`, `.ps1`, icon, version resource or release workflow exists. PyInstaller
  and Inno Setup are not installed on this machine.
- **No application version.** `pyproject.toml` holds only ruff/pytest settings; nothing defines `__version__`.
- **No production entry point.** `app/app.py:651-668` (`main`) is a development entry point: argparse, no logging setup,
  no exception hooks.
- **No log files.** Modules call `logging.getLogger(__name__)` (`db.py:180`, `sync/service.py:106`,
  `app_services.py:77`, …) but nothing configures a handler. In a windowed executable every warning, the
  `logger.exception` in the sync loop (`service.py:843`) and every traceback are lost.
- **No global exception handling.** There is no `sys.excepthook`, `threading.excepthook` or Tk
  `report_callback_exception`. An exception inside a Tk callback prints to a console that will not exist.
- **No backup before a schema migration.** Each migration is atomic, but a migration that succeeds with a logic bug has
  no recovery copy. Required before automatic updates ship.
- **No icon.** No `.ico`/`.png` is tracked; the window uses the default Tk icon (`app/app.py:251` sets only the title).
- **No update mechanism.**
- **CI is Linux-only** (`ci.yml`: every job is `ubuntu-latest`); nothing builds or tests on Windows.

### 1.3 What is unsafe for distribution

- **Direct PostgreSQL mode must not ship.** `app/app.py:653-667` accepts `--storage postgres --env-file`, and
  `SCHEDULE_MAXING_STORAGE` / `SCHEDULE_MAXING_ENV_FILE` select it from the environment (`config/settings.py:199-210`).
  It gives the client full database credentials (`.env.example` says so itself). The code is lazily imported
  (`app/persistence/__init__.py:37-47`), so the packaged build can exclude `backend/`, SQLAlchemy, psycopg, Alembic,
  argon2 and python-dotenv, and the frozen entry point must refuse the mode outright.
- **The checkout `.env` holds a real `DATABASE_URL`** (one key; value not read for this audit). It is git-ignored
  (`.gitignore:7-9`). A PyInstaller spec must never add the repository root or `.env*` as data, and the build should
  fail if a `.env` appears in the output.
- **Building from the full development venv would bundle the server.** `.venv` contains FastAPI, SQLAlchemy and psycopg.
  PyInstaller follows the lazy imports in `app/persistence/` and `app/ui/direct_services.py`, so the build needs a
  desktop-only environment plus explicit `excludes`.
- **`app/persistence/verify_render.py:11`** has a personal e-mail address in a docstring example. It is a developer
  tool, excluded from the package; noted only so it is not bundled.

### 1.4 What breaks under PyInstaller

- **`sys.stdout` / `sys.stderr` are `None` in a windowed build.** `argparse` (`app/app.py:652-666`, `parser.error`) and
  `app/ui/diagnostics.py:424-426` write to them and would raise `AttributeError`.
- **`config/task_preference.yaml` is found through `__file__`** (`app/reward.py:201-204`, `_default_project_root`).
  Frozen, that resolves inside the bundle; if the file is not shipped at `config/`, `_resolve_config_path` returns
  `None` (`:230`) and the app silently runs on built-in defaults. It must be bundled and the lookup made explicit.
- **`LEGACY_DATA_DIR`** (`config/settings.py:145`) resolves to `<bundle>/data`. Harmless (it will not exist) but legacy
  adoption (`db.py:1511-1512`, `ml_persistence.py:97-100`) should be switched off when frozen rather than probing the
  install directory.
- **Package data and hidden imports** that static analysis misses: CustomTkinter's theme JSON and assets, `tzdata`'s
  zone files, `keyring`'s Windows backend (entry-point discovery; needs `pywin32-ctypes`), scikit-learn/scipy compiled
  submodules, and `pydantic_core`.
- **`app/main.py:354,359,446`** reads `samples/inputs` and writes `samples/outputs` relative to the source tree. It is the
  developer CLI and is not part of the packaged app; it stays dev-only.

### 1.5 What breaks under Program Files

Nothing writes to the install directory today: every writer resolves through `settings.DATA_DIR`, an explicit
user-chosen path (`filedialog` in `app/ui/day_page.py:541,587`), or the temp directory (`diagnostics.py:112`). The
remaining risk is the legacy-adoption probe above, and any future code that writes beside `__file__`.

### 1.6 What needs refactoring (small, centralised)

- One runtime helper for "frozen or source", the bundled-resource root and the log directory, used by `reward.py` and
  `settings.py` instead of ad-hoc `__file__` arithmetic.
- One production bootstrap that configures logging and exception hooks, guards the frozen build against direct mode,
  and then calls the existing `app.app.main`.
- One version constant read by the About page, the executable metadata, the installer and the updater.

### 1.7 What should not change

- `app/execution/db.py` migration ordering, numbering and atomicity; `LATEST_SCHEMA_VERSION` semantics.
- The data directory name `ScheduleMaxing` and the filename `executions.db` (existing installs and overrides depend on them).
- `SCHEDULE_MAXING_DATA_DIR`, `SCHEDULE_MAXING_BACKEND_URL`, `SCHEDULE_MAXING_TIMEZONE` behaviour.
- `python -m app.app`, `python -m app.main`, `python -m app.web` as development entry points; the test suite uses them.
- The desktop/web/direct dependency boundaries and `tests/test_desktop_isolation.py`.
- Optimizer engines, reward, constraints, sync protocol, backend.

### 1.8 Leftovers

- Root `*.log` files are git-ignored (`.gitignore:29`) and untracked. Nothing reads them.
- `.diagnostic-deps/` is git-ignored (`.gitignore:3`), untracked, and referenced only by four historical documents under
  `docs/`. No code depends on it. Both are safe to delete locally; neither affects a build that uses an explicit spec.

---

## 2. File classification

| File | Kind | Today | Packaged target |
|---|---|---|---|
| `config/task_preference.yaml` | bundled, read-only | `<repo>/config/` via `reward.py:201-230` | `<install>\_internal\config\` via the runtime helper |
| CustomTkinter themes/assets | bundled, read-only | site-packages | `_internal\customtkinter\` |
| `tzdata` zone files | bundled, read-only | site-packages | `_internal\tzdata\` |
| Application icon | bundled, read-only | does not exist | `assets/ScheduleMaxing.ico` → `_internal\assets\`, and embedded in the exe |
| `executions.db` (+ `-journal`/`-wal`) | user data | `%LOCALAPPDATA%\ScheduleMaxing\` | unchanged |
| `executions.db.lock` | user data | beside the database | unchanged |
| `ui_settings.json`, `task_defaults.json` | user data | beside the database | unchanged |
| `ml_duration_model.joblib` + `.meta.json` | user data | data directory | unchanged |
| Refresh credential | user secret | Windows Credential Manager (`keyring`) | unchanged |
| Log files | user data | none | `%LOCALAPPDATA%\ScheduleMaxing\logs\schedule-maxing.log` (rotating) |
| Pre-migration backups | user data | none | `%LOCALAPPDATA%\ScheduleMaxing\backups\` |
| Update downloads | cache | none | `%LOCALAPPDATA%\ScheduleMaxing\updates\` |
| CSV import/export | user-chosen | file dialog | unchanged |
| UI diagnostics JSON (opt-in) | temp | `%TEMP%` or the env-var path | unchanged |
| `.env`, `backend/`, `app/web/`, `app/persistence/direct.py`, `samples/`, `benchmarks/`, `tests/`, `docs/` | not shipped | repository | excluded |

---

## 3. Proposed production architecture

```
 ScheduleMaxing-Setup-X.Y.Z.exe  (Inno Setup, x64)
        │ installs
        ▼
 C:\Program Files\Schedule Maxing\            read-only at run time
   ScheduleMaxing.exe        ← PyInstaller onedir, windowed, entry: app/desktop.py
   _internal\                ← Python runtime, app/, config/task_preference.yaml,
                               customtkinter, tzdata, sklearn/pandas, assets\ScheduleMaxing.ico
   unins000.exe
        │ reads/writes only
        ▼
 %LOCALAPPDATA%\ScheduleMaxing\               survives upgrade and uninstall
   executions.db  (SQLite, PRAGMA user_version, migrated on open)
   executions.db.lock
   ui_settings.json   task_defaults.json
   ml_duration_model.joblib  ml_duration_model.meta.json
   logs\schedule-maxing.log (+ rotations)
   backups\executions-v<N>-<timestamp>.db   (before a schema migration; a few kept)
   updates\  (downloaded installer, verified, removed after use)

 Windows Credential Manager  ← refresh credential ("Keep me signed in")

 ScheduleMaxing.exe ──HTTPS (urllib, 10 s timeout, background thread)──► Render: FastAPI ──► PostgreSQL
        │   app/sync: outbox push / change pull; local SQLite stays the working copy
        │
        └──HTTPS (background thread, after the window is up)──► GitHub Releases (latest stable)
               version compare → offer → download installer + SHA256SUMS → verify → run installer → exit
```

Principles:

- The client never holds `DATABASE_URL` or `JWT_SECRET`; PostgreSQL is reachable only through the API.
- Runtime detection lives in one module (`app/runtime.py`). Business logic does not test `sys.frozen`.
- Development keeps working unchanged: `python -m app.app`. `python -m app.desktop` runs the production bootstrap from source.
- Upgrades replace only the install directory. Uninstall leaves `%LOCALAPPDATA%\ScheduleMaxing` in place.

Native runtime: Python 3.10 needs the Universal CRT (part of Windows 10/11) and `vcruntime140.dll`, which PyInstaller
bundles from the build interpreter. No separate redistributable install is expected; this is to be confirmed on a clean
machine, not assumed. Supported targets for 1.0.0: Windows 10 x64 and Windows 11 x64, each only once actually tested.

---

## 4. Repository changes

Added:

- `app/version.py` — the single version source.
- `app/runtime.py` — frozen detection, resource root, log/backup/update directories.
- `app/desktop.py` — production bootstrap (logging, hooks, frozen guards, single-instance mutex), then `app.app.main`.
- `app/logging_setup.py` — rotating file logging and exception hooks.
- `app/execution/backup.py` — pre-migration backup and retention.
- `app/update/` — `versioning.py`, `release_feed.py`, `downloader.py`, `service.py`; `app/ui/update_controller.py` and a small prompt in the shell.
- `packaging/windows/`: `ScheduleMaxing.spec`, `launcher.py`, `version_info.py` (the exe version resource),
  `ScheduleMaxing.iss`, `build_windows.ps1`, `check_bundle.py`, `smoke_test.py`, `test_installer.py`, `checksums.py`,
  `make_icon.py`; and `assets/ScheduleMaxing.ico` at the repository root.
- `app/selftest.py` — the packaged program's self-test.
- `requirements-build.txt` — `-r requirements-desktop.txt` plus PyInstaller.
- `.github/workflows/release.yml`.
- `docs/windows-distribution.md` — build, release, update, rollback and manual verification.
- Tests: `tests/test_runtime.py`, `tests/test_version.py`, `tests/test_logging_setup.py`, `tests/test_desktop_entry.py`,
  `tests/execution/test_backup.py`, `tests/update/…`, `tests/test_packaging_config.py`.

Modified:

- `app/reward.py` (`_default_project_root` → runtime helper), `config/settings.py` (legacy directory off when frozen;
  update settings), `app/execution/db.py` (backup hook around `initialize_schema`), `app/app.py` (window icon, About
  text with version, update prompt wiring), `app/ui/ui_settings.py` (update-check preference), `app/ui/pages.py`
  (Settings toggle), `.gitignore` (`build/`, `dist/`, installer output), `README.md`, `docs/testing.md`.

Removed: nothing. No competing packaging or entry-point code exists to delete.

---

## 5. Decisions (recommendation in bold; the prompts assume the recommendation)

1. **Install scope.** **Per-machine under Program Files by default, with Inno Setup's per-user choice allowed**
   (`PrivilegesRequired=admin`, `PrivilegesRequiredOverridesAllowed=dialog`). It matches the stated requirement and the
   Program Files tests. Cost: an update shows one UAC prompt. Per-user-only would avoid UAC but installs under
   `%LOCALAPPDATA%\Programs`.
2. **Update mechanism.** **Download the newer Inno Setup installer and run it**, then exit. The installer already
   handles file replacement, elevation, closing the running app (`AppMutex`) and restart. No custom file replacement.
3. **Update source and verification.** **GitHub Releases `releases/latest`** (GitHub excludes drafts and pre-releases
   there), asset names fixed by pattern, with a `SHA256SUMS.txt` asset. Downloads only from an allow-list of GitHub
   hosts over HTTPS. The checksum proves integrity, not authorship: it comes from the same place as the installer.
   Authenticode verification is added when a certificate exists.
4. **Version source.** **`app/version.py`**; the build script and workflow read it, and the release workflow fails if
   the tag does not equal it.
5. **Direct PostgreSQL mode.** **Not shipped.** Excluded from the bundle and refused by the frozen entry point. It
   remains available from source for development.
6. **Entry point.** **`app/desktop.py`** (`python -m app.desktop`) is the one production entry point; `app.app.main`
   stays the UI and the development entry point it wraps.

Things only you can settle:

- **The repository must be public (or releases published from a public repository) for the updater to work.** An
  unauthenticated client cannot read releases of a private repository, and a token must never be embedded. Visibility
  could not be checked from here (`gh` is not installed).
- **Publisher name** for the installer and exe metadata. The prompts use the neutral "Schedule Maxing" until you choose.
- **Icon artwork.** A placeholder icon is generated so the pipeline works; replace it before 1.0.0.
- **Code-signing certificate.** Without one, SmartScreen will warn on download and install.

Nothing found makes a spec requirement infeasible or changes the suggested milestone order.

---

## 6. Milestones

| # | Prompt | Depends on | Outcome |
|---|---|---|---|
| 1 | `01-runtime-paths-entry-version.md` | — | Version source, runtime helper, production entry point, resource lookup |
| 2 | `02-persistence-logging-safety.md` | 1 | Log files, exception hooks, pre-migration backup, frozen guards, offline checks |
| 3 | `03-pyinstaller-build.md` | 1, 2 | Spec, icon, build script, packaged smoke test |
| 4 | `04-inno-setup-installer.md` | 3 | Installer, metadata, upgrade and uninstall behaviour |
| 5 | `05-release-workflow.md` | 3, 4 | Tag-triggered Windows release with checksums and a signing hook |
| 6 | `06-updater.md` | 1, 2, 4, 5 | Update check, verified download, installer hand-off, setting |
| 7 | `07-release-verification.md` | all | Release audit, fixes, 1.0.0 manual checklist |
