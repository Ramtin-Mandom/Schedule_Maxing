# Prompt 1 — Version source, runtime helper and production entry point

Implement only this prompt, then stop with a completion report. Read `CLAUDE.md`, `docs/milestone-9-prompts/README.md`
and `00-audit-and-architecture.md` (sections 1.2, 1.4, 1.6, 3, 5), inspect `git status` and the code named below before
editing. The checkout takes precedence over line numbers in the audit. Do not commit, push or read `.env`.

Objective: give the application one version, one place that knows whether it runs from source or from a packaged
executable, and one production entry point — without changing how the app behaves when run from source. No packaging,
installer, logging-to-file or updater work here (Prompts 2–6).

Audit findings addressed: no application version (1.2); no production entry point (1.2); `sys.stdout`/`sys.stderr` are
`None` in a windowed build and `argparse`/diagnostics write to them (1.4); `config/task_preference.yaml` is located by
`__file__` arithmetic in `app/reward.py:_default_project_root` and silently falls back to defaults when missing (1.4);
`config/settings.py:LEGACY_DATA_DIR` probes beside the code (1.4).

Inspect first: `app/app.py` (`main`, `ScheduleOptimizerApp.__init__`, `_PLACEHOLDERS`), `app/reward.py`
(`load_reward_settings`, `_resolve_config_path`, `_default_project_root`), `config/settings.py`,
`app/execution/db.py` (`get_connection`, `_adopt_legacy_for_default_location`),
`app/productivity/ml_persistence.py` and `ml_artifact_migration.py` (legacy adoption), `app/ui/diagnostics.py`
(`install_from_env`, the `print` calls), `tests/test_desktop_isolation.py` and `tests/isolation_probe.py`.

Required work:

1. `app/version.py`: `__version__ = "1.0.0"` plus `APP_NAME = "Schedule Maxing"` and `APP_ID = "ScheduleMaxing"`.
   Standard library only, importable without side effects, parseable by a build script with a simple regular
   expression. This is the only place the version is written.
2. `app/runtime.py`, standard library only and free of Tk and of every optional package:
   - `is_frozen()` — true for a PyInstaller build (`sys.frozen` and `sys._MEIPASS`).
   - `resource_root()` — the directory that holds bundled read-only resources: `sys._MEIPASS` when frozen, the
     repository root from source.
   - `resource_path(*parts)` — a path under it.
   - `logs_dir()`, `backups_dir()`, `updates_dir()` — under `config.settings.DATA_DIR` (resolved at call time, so the
     `SCHEDULE_MAXING_DATA_DIR` override and tests that patch settings keep working). They return paths; they do not
     create directories on import.
   - `ensure_standard_streams()` — when `sys.stdout` or `sys.stderr` is `None`, replace it with a writable null stream.
   Keep functions injectable (parameters for `sys`-like state) so tests cover the frozen branch without freezing.
3. `app/reward.py`: `_default_project_root` uses `runtime.resource_root()`. Behaviour from source is identical. Keep the
   legacy-filename search and the explicit `project_root=` / `config_path=` overrides exactly as they are.
4. `config/settings.py`: when frozen, legacy adoption from the checkout must not happen. Do this in one place (for
   example a `LEGACY_ADOPTION_ENABLED` flag consulted by `db._adopt_legacy_for_default_location` and
   `ml_persistence.load_model_artifact` / `ml_artifact_migration.migrate_default_ml_artifact`), not by scattering
   `is_frozen()` checks. From source the behaviour and its tests are unchanged.
5. `app/desktop.py` — the production entry point, runnable as `python -m app.desktop` and usable as the PyInstaller
   script target:
   - call `runtime.ensure_standard_streams()` before anything can print;
   - when frozen: ignore `SCHEDULE_MAXING_STORAGE` and `SCHEDULE_MAXING_ENV_FILE`, accept no `--storage`/`--env-file`,
     and always start with local storage (audit 1.3, decision 5). From source, `python -m app.desktop` also starts local
     storage only; direct mode stays reachable through `python -m app.app` for development;
   - construct `ScheduleOptimizerApp(storage="local")` and run its main loop;
   - leave clearly marked seams (small functions) where Prompt 2 installs logging and exception hooks, and where
     Prompt 6 starts the update check. Do not implement those here.
   Keep it thin: no business logic, no duplicated startup code from `app/app.py`.
6. About page: show the version (`Schedule Maxing 1.0.0`) using `app/version.py`. Keep the existing wording otherwise.
7. Documentation: a short "Entry points" note in `README.md` — `python -m app.desktop` (production bootstrap),
   `python -m app.app` (development, including direct mode), `python -m app.main` (CLI), `python -m app.web` (local web).

Constraints: do not rename or remove `app.app.main`; do not change `DATA_DIR`, the database filename, environment
variable names or defaults; no new dependencies; no `sys.frozen` checks outside `app/runtime.py` and `app/desktop.py`.

Tests to add (fast, headless, in the `dev` tier):

- `tests/test_version.py`: the version is a valid `MAJOR.MINOR.PATCH` string; it is defined in exactly one place
  (search the repository's Python, spec and installer sources for a second hard-coded copy).
- `tests/test_runtime.py`: source and simulated-frozen branches of `resource_root`/`resource_path`; data-derived
  directories follow a patched `DATA_DIR`; `ensure_standard_streams` replaces `None` streams and leaves real ones alone;
  importing `app.runtime` creates no directory.
- Reward config: found through `resource_root()` from source; a simulated frozen root containing `config/task_preference.yaml`
  is used; a root without it still yields defaults (existing behaviour).
- Legacy adoption is skipped when the frozen flag is set, and still happens from source (extend the existing tests
  rather than duplicating them).
- `tests/test_desktop_entry.py`: with the frozen flag simulated and `SCHEDULE_MAXING_STORAGE=postgres` set, the entry
  point still chooses local storage; stream replacement happens before the app is built. Use a stub in place of the real
  window so the test opens no Tk window.

Verification: focused tests for the files above, then once `python -m pytest -m dev` and `python -m ruff check .`.
Launch `python -m app.desktop` and `python -m app.app` once by hand (or through the existing isolation probe) to
confirm both still open.

Completion report: files changed; the public surface of `app/runtime.py`, `app/version.py` and `app/desktop.py` that
Prompts 2, 3 and 6 will use; commands and results; anything not verified.
