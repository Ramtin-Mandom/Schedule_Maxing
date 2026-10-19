---
name: schedule-maxing-debugging
description: How to debug and test Schedule Maxing cheaply. Use when a test fails locally or in CI (including the GitHub workflow YAML tests), when a CI job fails but tests pass locally, when writing tests for a fix, or when the installed Windows app or a release misbehaves on someone else's computer but works from source.
---

# Debugging and testing Schedule Maxing

Goal: find the cause with the fewest test runs and file reads. Every full-suite run costs about 13 minutes and a lot
of context; never use it as a search tool.

## 1. Pick the smallest run that can answer the question

| Situation | Run |
|---|---|
| One failing test | `python -m pytest tests/path/test_x.py::test_name -x -q` |
| Tests around a change | `python -m pytest tests/path/test_x.py -k keyword -q` |
| Batch finished | `python -m pytest -m dev -q` (about 2 min) and `python -m ruff check .`, once |
| Milestone / merge / release | `python -m pytest`, `python -m compileall .`, `ruff check .`, once |
| Rerun only what failed last time | `python -m pytest --lf -q` |

Add `-p no:cacheprovider` only if the cache itself is suspect. Read the failure's last traceback frame and assertion
first; open the test file only at that test, and the code only at the frame it points to.

## 2. Classify the failure before fixing anything

1. **Assertion on behaviour you just changed:** decide which is right, the test or the code. Update a test only when
   the task deliberately changed that behaviour. Never weaken or delete a meaningful test to get green.
2. **Fails only in the full suite, passes alone:** shared state. Suspects: Tk windows/timers left open
   (`tests/tk_cleanup.py`), a module-level cache, `config.settings.DATA_DIR` not isolated, an environment variable a
   test set without `monkeypatch`.
3. **`TclError: invalid command name "tcl_findLibrary"`** in a real-window test: the known flake
   (`docs/testing.md`). Rerun that test alone; if it passes, it is the flake, not your change.
4. **Skipped locally, failing in CI:** `-m postgres` and the `BACKEND_TESTS_ON_POSTGRES=1` suites need a disposable
   `TEST_DATABASE_URL`. Start one with `docker compose -f docker-compose.postgres-test.yml up -d` and use
   `TEST_DATABASE_URL=postgresql://sm_test@127.0.0.1:55432/schedule_maxing_test`. Never point it at a real database.
   Remove the container afterwards with `docker compose -f docker-compose.postgres-test.yml down -v`.
5. **Passes on Windows, fails in CI (Linux, xvfb):** path separators or case, `\r\n` vs `\n`, a Windows-only API
   (Credential Manager, `%LOCALAPPDATA%`), timing in UI tests, a font or DPI difference.

## 3. Which CI job failed tells you the cause

| Job (`.github/workflows/ci.yml`) | Usual cause |
|---|---|
| `test` | A real test failure; reproduce with the exact test id from the log |
| `desktop-only` | An `app/` module imports a server package (fastapi, sqlalchemy, psycopg, alembic, jwt, argon2, httpx, uvicorn) at top level, or a test outside `tests/backend|sync|web|direct` needs one. Make the import lazy or move the test |
| `direct-desktop` | Direct mode imported an HTTP/JWT package, or a direct-mode test depends on SQLite-only behaviour |
| `postgres` | SQL that works on SQLite but not PostgreSQL: types, ordering without `ORDER BY`, case-sensitive comparisons, timezone-naive timestamps, constraint names |
| `security-scan` / pip-audit | A vulnerable pinned version in `requirements.lock`: upgrade it and the matching `requirements-*.txt` |
| `security-scan` / detect-secrets | A new secret-looking string. If it really is not a secret, update the baseline (`detect-secrets scan --baseline secret-scan-baseline.json`) and review the diff; if it is one, remove it and rotate it |

To reproduce `desktop-only` locally, use a throwaway venv with only `requirements-desktop.txt` and `pytest`, then run
`python -m pytest --ignore=tests/backend --ignore=tests/sync --ignore=tests/web --ignore=tests/direct`.

## 4. The workflow YAML tests

`tests/test_workflows.py`, `tests/test_release_workflow.py` and `tests/test_deploy_workflow.py` parse
`.github/workflows/*.yml` with `tests/workflow_files.py`. They find a job by what its steps run (for example the one
running `gh release create`) and its order by `needs`, so renaming a job or rewording a step does not break them.
Losing a protected property does. When one fails, read the assertion message, then fix the **workflow**, not the
test, unless the property is deliberately being changed.

Rules every workflow must keep:

- `jobs` exist, and every `needs` names a real job.
- No `continue-on-error` anywhere.
- No `${{ ... }}` inside a `run:` script. Pass it in through `env:` and use `"$VAR"`.
- Every `uses:` is pinned: `actions/*` and `github/*` to `@vN`, any other publisher to a full 40-character commit SHA.
- Secrets appear only as `${{ secrets.NAME }}`, never as values.
- The workflow-level `permissions` never grants `write`. Only the publishing job may write, and only for tags.
- Every repository path a step names (`tests/...`, `packaging/...`, `.github/scripts/...`, `requirements*.txt`) must
  exist. Renaming or moving a file means updating the workflow in the same change.
- No database address and no `.env` file in deploy/release workflows.
- Release: started only by `v*` tags or a manual run. `publish` needs every earlier job. The tag must equal
  `app/version.py`. Tests are skipped only through the `SKIP_RELEASE_TESTS` repository variable.
- Deploy: only a push to `main` or a manual run. It deploys only a commit whose pull request passed CI
  (`.github/scripts/pr_tests_passed.sh`). It uses the protected environment, one deploy at a time.

YAML gotcha: PyYAML reads the key `on` as the boolean `True`. Use `workflow_files.triggers(workflow)`, never
`workflow["on"]`. A new workflow is checked automatically, so run the three files above after adding or editing any
workflow: `python -m pytest tests/test_workflows.py tests/test_release_workflow.py tests/test_deploy_workflow.py -q`.

Commits pushed straight to `main` are never tested (deploy and release reuse the pull request's CI result). Change
code through pull requests, and do not use `[skip ci]` on a pull request that will be released.

## 5. "Works for me, broken for someone else" (installed app / release)

Packaged and source runs differ. Check these in order:

1. **Get their log and version.** `%LOCALAPPDATA%\ScheduleMaxing\logs\schedule-maxing.log` (plus 5 rotated files),
   the About page version, and Windows 10/11. Ask them for these before guessing.
2. **Reproduce the packaged program, not the source.** `packaging\windows\build_windows.ps1 -SkipInstaller`, then run
   `dist\ScheduleMaxing\ScheduleMaxing.exe --self-test report.json` with `SCHEDULE_MAXING_DATA_DIR` set to an empty
   scratch folder. It checks resources, compiled deps, DB creation, every scheduling mode, CSV round trip, restart and
   every page.
3. **Resource missing in the bundle:** a new file under `config/` or `assets/`, or a new dependency (or one of its data
   files or hidden imports), not listed in `packaging/windows/ScheduleMaxing.spec`. Code must use
   `runtime.resource_path(...)`, never a path relative to `__file__` or the current directory.
4. **Writes fail:** code writing next to the program (Program Files is read-only) instead of under
   `runtime.data_dir()`. A path with spaces or non-ASCII characters (user name) not quoted or encoded. Always pass
   `encoding="utf-8"`. Their default code page is not yours.
5. **Their data is different from yours:** an old database being migrated (`app/execution/db.py`; a backup is made in
   `backups\` first). A database from a *newer* version is refused. Settings JSON from an older version is missing a
   key. Test the upgrade path: install the previous release, use it, then install the new one
   (`packaging/windows/test_installer.py <installer>` automates this).
6. **Account / sync:** Render's free instance sleeps, so the first request can time out. Check `DEFAULT_BACKEND_URL` in
   `config/settings.py`. Check whether email sends (Render blocks SMTP; Brevo/Mailjet over HTTPS). A clock or timezone
   difference can show up as token expiry or a day boundary off by one; check `tzdata` is bundled.
7. **Updater:** it only offers a *stable*, *newer* `vX.Y.Z` release with exactly one installer and `SHA256SUMS.txt`.
   A wrong asset name, a pre-release, or a checksum mismatch means "no update" or a refused download.
8. **Only on their machine:** SmartScreen or antivirus quarantining the unsigned exe. A second copy already running
   (instance lock). A build from another Python than CI's 3.10 (`build_windows.ps1` now refuses one, and an
   old `.venv-build` of another version, until rebuilt with `-Clean`; an installer built before that check may differ).

A bad release that has already shipped: mark it pre-release on GitHub so it stops being offered, then publish a
higher patch version with the fix. There is no automatic downgrade.

## 6. Writing the test for a fix

- Reproduce the bug as a failing test first, at the lowest layer that shows it (domain test over a UI test).
- Put it next to the existing tests for that module. Tiers are assigned automatically (`tests/test_tiers.py`). Mark
  a test over about a second `@pytest.mark.slow` so `-m dev` stays fast.
- Isolate state: `tmp_path` for files, `monkeypatch` for env vars and `config.settings.DATA_DIR`, no real network.
  Backend tests get their own migrated DB copy from `tests/db_template.py`.
- A bug that only showed up in the packaged app gets a check in `app/selftest.py` or `check_bundle.py` too, so the
  build catches it next time.
