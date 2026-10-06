# Prompt 2 — Logging, crash handling, migration backup and offline safety

Implement only this prompt, then stop with a completion report. Prerequisite: Prompt 1 (`app/version.py`,
`app/runtime.py`, `app/desktop.py`). Read `CLAUDE.md`, `docs/milestone-9-prompts/README.md` and
`00-audit-and-architecture.md` (sections 1.1–1.3, 2, 3), inspect `git status` and the code below before editing. Do not
commit, push or read `.env`.

Objective: an installed app with no console must leave a usable log, show a readable error instead of vanishing, never
lose a database to a migration, and start with or without a network. The persistence design is already sound (audit
1.1); add the missing safety net, do not redesign it.

Audit findings addressed: no log handler anywhere (1.2); no `sys.excepthook`, `threading.excepthook` or Tk
`report_callback_exception` (1.2); no backup before a schema migration (1.2); direct mode must be unreachable in the
packaged app (1.3).

Inspect first: `app/execution/db.py` (`initialize_schema`, `get_connection`, `MIGRATIONS`, `MigrationError`,
`IntegrityCheckError`, `adopt_legacy_database` for the backup-API pattern), `app/ui/app_services.py`
(`open_app_services`, `describe_startup_failure`), `app/app.py` (`__init__`, `_build_startup_error`), `app/sync/service.py`
(`start`, `_run`, `sync_now`, `restore_session`, every `logger.` call), `app/sync/transport.py` (`HttpTransport._request`),
`app/sync/credentials.py`, `app/ui/background.py` (`WorkerRegistry._work`), `app/ui/ui_settings.py`,
`app/ui/task_defaults.py`, `app/desktop.py`, and the existing migration tests under `tests/execution/`.

Required work:

1. `app/logging_setup.py` (standard library only):
   - `configure_logging(log_dir=None)` adds one `RotatingFileHandler` (about 1 MB, 5 backups, UTF-8) writing
     `schedule-maxing.log` under `runtime.logs_dir()`. Idempotent. If the directory or file cannot be created, the app
     still starts (fall back to no file handler) — logging must never be a startup failure.
   - A first line per start with the version, frozen or source, Python and Windows versions, the data directory and the
     schema version. Never log passwords, access or refresh tokens, `Authorization` headers, e-mail addresses in full,
     or `DATABASE_URL`.
   - `install_exception_hooks(show_error=None)` sets `sys.excepthook` and `threading.excepthook` to log the traceback;
     the main-thread hook also calls `show_error` so the user sees a short dialog naming the log file.
   - A helper the Tk root uses for `report_callback_exception`: log the traceback, show one dialog, keep the app alive.
     Guard against a dialog storm (the same error repeating).
2. Wire these into the seams left in `app/desktop.py`: logging first, hooks next, then the window. `python -m app.app`
   stays unchanged (console development), except that `ScheduleOptimizerApp` may accept an optional error reporter.
3. Audit every existing `logger.` call in `app/sync`, `app/execution`, `app/ui` for sensitive values and fix any that
   could log a credential. Report what you checked.
4. Pre-migration backup, `app/execution/backup.py` plus a minimal hook in `get_connection`:
   - when an existing on-disk database has pending migrations (`0 < user_version < LATEST_SCHEMA_VERSION`), copy it with
     SQLite's online backup API to `runtime.backups_dir() / "executions-v<old>-<UTC timestamp>.db"` before
     `initialize_schema` runs; write to a temporary name and rename, so a partial backup is never mistaken for a good one;
   - keep the newest few (5) backups, delete older ones; never touch anything else in the data directory;
   - a brand-new database, an in-memory database and an already-current database create no backup;
   - if the backup cannot be written (disk full, permissions), do not migrate: raise a `StorageError` subclass with a
     clear message, leaving the database untouched;
   - log the backup path and each migration step.
   Do not change migration numbering, ordering or atomicity.
5. Startup error messages (`describe_startup_failure`): give specific, plain guidance for (a) a database newer than the
   app ("installed version is older than your data; install the latest version"), (b) a failed migration (name the
   backup and the log), (c) a failed backup, (d) an unreadable or corrupt database file. Keep the existing in-use and
   generic messages. Never offer to delete the user's data.
6. Corrupt or missing local settings: confirm `ui_settings.json` and `task_defaults.json` fall back to defaults without
   raising and log a warning; fix only if they do not.
7. Offline behaviour — verify by reading and by test, and fix only real defects:
   - no network call on the Tk thread during startup or shutdown;
   - with the backend unreachable, timing out or returning 5xx, the window opens and local work is saved;
   - a failed sign-in reports an error on the Account page and leaves local data usable;
   - sync resumes after connectivity returns (back-off), without restarting the app.
8. Single-instance signal for the installer: in `app/desktop.py`, on Windows, create a named mutex
   (`ScheduleMaxing_SingleInstance`) with `ctypes` and hold it for the process lifetime. It is only a signal for the
   installer's `AppMutex` (Prompt 4); the existing database instance lock keeps deciding whether a second window may
   open. Failure to create it is ignored.

Constraints: no new dependencies; hard constraints, sync protocol and backend untouched; no `sys.frozen` checks outside
`app/runtime.py` and `app/desktop.py`.

Tests to add (headless, `dev` tier unless they need a real window):

- Logging: file created under a patched data directory; rotation configured; idempotent; an unwritable directory does
  not raise; a secret-looking value passed through the documented logging paths does not reach the file.
- Hooks: an exception on the main thread and one on a worker thread are both logged with a traceback; the reporter is
  called once for a repeated Tk-callback error.
- Backup: created for a genuine older-version database (build it with `initialize_schema(target_version=...)`), named
  with the old version, restorable and readable; none for new, current or in-memory databases; retention keeps 5;
  a failing backup prevents migration and leaves `user_version` and the data unchanged.
- Migration failure after a successful backup: the database stays at the old version, the backup exists, the error
  message names it. Reuse the existing failing-migration test pattern.
- Newer-than-supported database: refused, untouched, specific message.
- Offline: `open_app_services` with a transport that raises `TransportError` on every call starts and completes a local
  create/read; with a transport that sleeps past its timeout the call returns without blocking the caller thread.
- Entry point: the frozen bootstrap installs logging before building the window (stubbed window, no Tk).

Verification: focused tests, then once `python -m pytest -m dev` and `python -m ruff check .`. By hand: start
`python -m app.desktop` with `SCHEDULE_MAXING_DATA_DIR` pointing at a scratch folder and with
`SCHEDULE_MAXING_BACKEND_URL` set to an unreachable address; confirm the window opens and a log file appears.

Completion report: files changed; log location and format; backup naming and retention; the list of logging call sites
reviewed for secrets; offline findings (confirmed or fixed); commands and results; anything not verified.
