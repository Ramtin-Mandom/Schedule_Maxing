# CI check notes

The memory of `/ci-check` (`.claude/skills/ci-check/SKILL.md`). It reads this file first on every run, so it
knows which contracts changed on purpose and what about CI differs from a Windows checkout without having to
work it out again from failure logs.

- **Pending:** structural changes CI has not been checked against yet. Add one in the same change that makes it
  (CLAUDE.md, "CI check notes"); `/wrap` adds any that were missed.
- **Checked:** entries `/ci-check` has dealt with, with the outcome. Keep the latest ten; delete older ones.
- **CI environment facts:** lasting differences between CI and a local Windows checkout. Add a fact whenever a CI
  failure turns out to come from one.
- **Open CI items:** known red or unverified things and decisions waiting on the owner.

## What counts as a structural change

- a public function, class, method or module renamed, moved, removed, or with a new signature or return shape;
- a changed default, constant, settings key or environment variable, enum value, or storage/JSON/DB field;
- user-visible text, a page or a widget name that tests look up;
- a file or directory renamed, moved or deleted (workflows, packaging and scripts name paths);
- a new background thread, process, executor, timer or `after()` loop, or a change to how one is stopped;
- new real-window behaviour (minimize, focus, geometry, DPI, fonts) or a new Tk event binding;
- a new or removed dependency, or an import that crosses the desktop/web boundary;
- a change to `.github/workflows/`, `.github/scripts/` or `packaging/`.

## Entry template

```markdown
### YYYY-MM-DD: <short title> (<commit, or branch if uncommitted>)
- Changed: <old> -> <new> (`path`, `symbol`)
- Intent: <why it was deliberate: the request, commit or ADR-NNN>
- Depends on it: <tests, workflow steps, packaging or requirement files that name the old contract>
- CI risk: <thread/process left running, real window, timing, ordering, Linux path, dependency boundary, none>
```

## Pending

## Checked

### 2026-10-27: First CI run of engine-remake (run 40083410138, abd5da7)
- Changed: nothing new; the run checked the dependency/priority removal (entry below) on CI.
- Outcome: `postgres`, `direct-desktop` (Alembic 0021 on PostgreSQL 17) and `security-scan` passed. Two test
  failures, both uncommitted fixes:
  - stale test (`desktop-only`): `tests/ui/test_projects_allocation.py::test_project_filter_is_display_only_after_scheduling_a_date`
    still asserted "predecessor ends before successor" after d111c3c dropped the dependency that guaranteed it.
    The two tasks tie, so allocation orders them by random id (failed 3 of 8 local runs). Now asserts the
    still-promised contract: both placed, no overlap.
  - CI environment (`test` and `desktop-only`): `tests/ui/test_desktop_shell.py::test_dialogs_drawers_and_the_task_list_work_from_the_keyboard`
    pressed Enter after focus-forcing the danger `ConfirmDialog` window itself; under X that FocusIn sends the
    focus back to Cancel (`ModalDialog.present`'s `<FocusIn>` handler), so Enter cancelled. Enter now goes to the
    dialog's body; the assertion is unchanged. Needs the next CI run to confirm (Linux only).

### 2026-10-23: Task dependencies and priority removed everywhere; points-first allocation (92c6993)
- Changed: local schema v18 -> v19 (`app/execution/db.py` `_V19_STATEMENTS`: `task_dependencies` dropped, `priority`
  dropped from `tasks` and `executions`; `ALTER TABLE ... DROP COLUMN` needs SQLite >= 3.35); server Alembic head
  `0020` -> `0021_drop_priority_and_dependencies` (project `default_priority` too; `0016` has a frozen
  `DEFAULT_TASK_PRIORITY`); `Task.priority`/`dependency_ids` and `TaskExecution.priority` removed (pydantic ignores
  them if passed); deleted `app/pert.py`, `app/planning/external_dependencies.py`, six `samples/inputs/dependency_*`
  / `priority_*` CSVs; `ExecutionFact` moved to `app/planning/execution_facts.py`. Wire placeholders kept:
  `LEGACY_TASK_WIRE_FIELDS` (`backend/resources.py`, `app/sync/mapping.py`), `LEGACY_EXECUTION_WIRE_FIELDS`
  (`backend/executions.py`). Canonical CSV `FORMAT_VERSION` 2 -> 3 (`IMPORTABLE_FORMAT_VERSIONS = ("2", "3")`);
  reset preview lost `blocking_dependents`/`blocked`. `app/reward.py` rebuilt: typed `ScoringTask`/`Neighbour`;
  removed `calculate_schedule_score`, `task_weights`, `task_time_windows`, `LEGACY_CONFIG_FILENAMES` and the legacy
  config filenames; `preferences.to_legacy_scoring_task` deleted; `_Placed` lost `name`/`time_window`.
  `provenance.FINGERPRINT_VERSION` 1 -> 2 and points are hashed. `allocation._allocation_order` = (-points,
  deadline, -duration, id). UI text: Settings "Priority weight" -> "Task weight"; the guide's "dependencies"
  section removed and its points text changed.
- Intent: ADR-048, ADR-049 (owner chose "everywhere" and points-first allocation).
- Depends on it: `tests/execution/test_migration_v19.py`, `tests/execution/conftest.py` (`without_dropped_columns`),
  `tests/ui/test_data_compatibility.py`, `tests/backend/test_migrations.py`, `tests/sync/test_protocol.py`,
  `tests/ui/test_guide.py`, `tests/ui/test_settings_*.py`, `tests/planning/test_allocation.py`,
  `benchmarks/results/prompt1_scheduling_mode_baseline.json` (week results regenerated).
- CI risk: SQLite version on the runner (needs >= 3.35 for DROP COLUMN; ubuntu-latest ships newer); the
  `postgres` job runs 0021 on real PostgreSQL (not run locally).
- Outcome: stale tests updated (uncommitted, 2026-10-24): hollow dependency leftovers renamed or given a
  real assertion (`tests/sync/test_two_devices.py::test_rescheduling_cleanup_and_completed_work_sync`,
  `tests/sync/test_milestone3_end_to_end.py` step 8, `tests/planning/test_application_service.py`,
  empty section in `test_allocation_service.py`, docstrings; `tests/ui/test_day_page.py` `test_csv_v2_...` renamed
  `test_canonical_csv_...`, the canonical CSV is format 3). `security-scan` (predicted): the regenerated
  `prompt1_scheduling_mode_baseline.json` records the new revision hash, and two baselined lines moved
  (`docs/render-deployment.md` 73->72, `tests/direct/test_direct_services.py` 290->291); baseline fixed by
  hand, hook exit 0. `postgres`/`direct-desktop` on PostgreSQL (revision 0021): checked statically
  (constraint names match 0001/0004, no triggers or views), needs the next CI run.

### 2026-10-23: Greedy Optimizer v1 removed (f47a080)
- Changed: deleted `app/models.py`, `app/constraints.py`, `app/data_processor.py`, `optimize_day_schedule` and its
  helpers, `RewardConfigPage`, the v1 tests and `benchmarks/optimizer_baseline.py`/`final_comparison.py` with
  their results; dead `config/settings.py` constants removed.
- Intent: ADR-047.
- Depends on it: `packaging/windows/check_bundle.py` / `ScheduleMaxing.spec` if they name the deleted modules;
  `tests/test_packaging_config.py`.
- CI risk: none expected (no app path called v1).
- Outcome: checked statically. No workflow, packaging or test path names the deleted modules; the full
  suite passed locally apart from the known flakes.

### 2026-10-23: ML duration predictor removed (a7cdf17)
- Changed: deleted `app/productivity/ml_*.py`, `predictor_comparison_cli.py`, `tests/productivity/test_ml_*.py`,
  `ml_fixtures.py`; removed `ProductivityService.predict_duration_comparison`, the controller method of the same
  name, `DurationPredictionComparison`, `settings.ML_MODEL_FILENAME`/`ML_MODEL_META_FILENAME`; dropped pandas,
  scikit-learn and joblib from `requirements-desktop.txt` and their pins (plus numpy, scipy, threadpoolctl, pytz,
  python-dateutil, six) from `requirements.lock`; the self-test no longer checks them.
- Intent: the owner does not plan machine learning on the data (ADR-037 superseded).
- Depends on it: `packaging/windows/check_bundle.py` required modules, `ScheduleMaxing.spec` excludes,
  `tests/test_startup_cost.py` (ML test removed, startup test renamed), `tests/backend/test_planning_api.py`.
- CI risk: dependency boundary -- CI and the Windows build must reinstall from the requirement files; the bundle
  check no longer expects sklearn/pandas.
- Outcome: checked statically (no `app/` import of pandas/sklearn/joblib; `check_bundle.py` and the spec
  match). `desktop-only` and the Windows build need the next CI run / release build.

### 2026-10-23: Secret-scan baseline line numbers (b7ac7a4)
- Changed: lines added above baselined test values in `.github/workflows/ci.yml` and
  `tests/backend/test_api_security.py`.
- Outcome: CI environment. `security-scan` failed with exit 123, "The baseline file was updated" (no new secret).
  Moved the three `line_number`s in `secret-scan-baseline.json` (66→67, 292→295, 392→395).

### 2026-10-22: Day engine in a separate process (1612f65, fixed in a49e77d)
- Changed: generations call the engine through `run_engine()`; `EngineProcess` (`app/planning/engine_process.py`)
  starts one spawned worker in `ScheduleOptimizerApp._warm_up` (1.5 s after the window opens).
- Intent: ADR-041/042 round-3 performance work.
- Depends on it: `tests/test_desktop_isolation.py` (no thread may outlive `AppServices.close()`),
  `tests/test_engine_process.py`; `tests/conftest.py` turns the process off for widget tests.
- Outcome: regression. `shutdown(wait=False)` left the pool's manager thread (`Thread-N`) running after close. It
  showed only on CI, where the worker started before the launch probe closed the app. Fixed with
  `shutdown(wait=True)` and a test that shuts down a still-starting worker.

### 2026-10-22: Window activity follows a real minimize (3c46092)
- Changed: `app/app.py` binds `<Map>`/`<Unmap>` and treats an unmap with `state() == "iconic"` as minimized.
- Depends on it: `tests/ui/test_activity_window.py::test_the_window_iconified_for_real_is_minimized`.
- Outcome: CI environment. `xvfb-run` has no window manager, so `iconify()` never unmaps the window. The test now
  uses the `window_manager` fixture (it starts Openbox), and `ci.yml` installs `openbox`. Confirmed green on CI
  (run 39885422774).

## CI environment facts

- **No window manager under `xvfb-run`.** `iconify()`, `wm state`, focus changes and WM placement do nothing. A
  test that needs them requests the `window_manager` fixture (`tests/ui/test_activity_window.py`; move it to
  `tests/ui/conftest.py` once a second file needs it). Never skip such a test instead.
- **Linux timing differs.** A spawned process starts faster than on Windows, and xvfb paints at a different speed.
  A race that Windows loses can be won on CI, or the other way round. Reason about the race; a local pass proves
  nothing.
- **Leftover threads fail `tests/test_desktop_isolation.py`.** Anything started during launch (threads, executors,
  `multiprocessing` pools) must be joined by `AppServices.close()`. Pool manager threads are named `Thread-N`.
- **Frozen test clocks plus `uuid4` ids give random order.** Many repositories order by `(created_at, id)`. With a
  fixed clock the tie-break is a random id, so an order assertion passes about 1 in n! of runs, on any platform.
- **CI installs version ranges, not the lock.** The `test`/`desktop-only`/`direct-desktop` jobs install
  `requirements*.txt`, so they can get a newer release than the local `.venv` (customtkinter 6 broke CI, b5668ff).
  `pip install --dry-run --ignore-installed -r <file> --report <path>` shows what CI would resolve.
- **The jobs see different packages.** `desktop-only` has no web/server packages; `direct-desktop` has no HTTP/JWT
  packages; only `postgres` and `direct-desktop` have a real PostgreSQL.
- **A failure log shows one job at a time.** `concurrency: cancel-in-progress` cancels the older run when you push
  again, and jobs fail independently, so a pasted log rarely shows everything. Cancelled or skipped jobs count as
  unknown, not as passing.
- **Moving a baselined line fails `security-scan`.** `secret-scan-baseline.json` records the line number of every
  accepted test credential. Adding or removing lines above one makes `detect-secrets-hook` rewrite the baseline and
  exit 123, even though no new secret appeared. Fix the moved `line_number`s by hand. Never let the hook rewrite the
  baseline on Windows: it writes backslash paths and CRLF, which CI then rejects. To check, run the hook against a
  copy of the baseline in the scratchpad (it refuses an unstaged baseline in place).
- **A regenerated scheduling-mode baseline adds a "secret".** `benchmarks/results/prompt1_scheduling_mode_baseline.json`
  records the git revision it ran from; a new 40-hex revision is a new `Hex High Entropy String` for
  `security-scan`. Update that entry's `hashed_secret` (the SHA-1 of the revision string) and `line_number` by
  hand in `secret-scan-baseline.json`.
- **Focus-forcing a Tk toplevel under X sends it a real FocusIn.** On Windows `toplevel.focus_force()` leaves the
  focus on the toplevel quietly; under xvfb the X server answers with a FocusIn some time later, which runs the
  toplevel's `<FocusIn>` bindings (a `ModalDialog` then moves the focus to its default button). A test that
  presses a key "on the dialog" should send it to a frame inside it, not to the toplevel.
- **Known Tcl flake:** `TclError: invalid command name "tcl_findLibrary"` in a real-window test. Rerun the test
  alone; if it passes, it was the flake.

## Open CI items

- `Deploy backend` failed on `main` for `9e37656` and `dfd53d6`: "Render accepted the request without a deploy
  id; check the Render dashboard" (not a test failure). Waiting on the owner: check the Render service and
  the `production` environment secrets.
- `engine-remake`: the two test fixes of 2026-10-27 are uncommitted; the dialog fix is confirmed only by the
  next CI run (`test`, `desktop-only`).
- Starlette warns "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead". Waiting on
  the owner: adding `httpx2` means a new dependency in `requirements.txt` and `requirements.lock`.
- `gh` is installed and logged in, but a shell opened before the install has the old PATH. If `gh` is not found,
  call `C:\Program Files\GitHub CLI\gh.exe` directly.
