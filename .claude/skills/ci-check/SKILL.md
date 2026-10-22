---
name: ci-check
description: After a change, bring the CI test suites in line with it and run them the way CI does, so a red CI run means a real bug and not a stale test. Finds tests, workflows and requirement files that reference changed or removed functions, classes, paths, settings or UI text; updates the ones that encode the old intended behaviour, adds tests for uncovered behaviour, reports real regressions, and runs the local equivalent of every CI job the change touches. Also accepts a CI failure log (pasted, a file, or a GitHub Actions run id) and triages it without reading it whole. Never commits or pushes.
argument-hint: "[optional: base ref, 'quick', a focus, a pasted CI error log, a log file path, or a GitHub Actions run id/URL]"
disable-model-invocation: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git merge-base:*)
  - Bash(git ls-files:*)
  - Bash(git rev-parse:*)
  - Bash(gh run view:*)
  - Bash(gh run list:*)
  - Bash(.venv/Scripts/python -m pytest:*)
  - Bash(.venv/Scripts/python -m ruff:*)
  - Bash(.venv/Scripts/python -m compileall:*)
---

# /ci-check — make CI test the change, not the past

Arguments (may be empty): $ARGUMENTS

A CI failure must mean "the code is wrong". It must not mean "a test still expects a function, path, label or value
that was deliberately changed". Your job is to make every test that CI runs agree with the **intended** current
behaviour, then run those suites locally like CI does, and report what is left.

## Rules (non-negotiable)

- **Never weaken a test to get green.** Updating a stale test means changing what it expects (a new name, signature,
  path, value or label) while keeping it just as strict: the same number of meaningful assertions, the same edge
  cases. Never delete an assertion, loosen `==` to `in`/`>=`, add `skip`/`xfail`, widen a tolerance, catch an
  exception that used to propagate, or drop a test from a workflow.
- **Never "fix" a test that caught a real bug.** If a failure comes from behaviour nobody meant to change, it's a
  regression: report it with the failing test id and the cause. Fix the application code only if the user asks.
- **Intent decides, not the diff.** A test is stale only when the change was deliberate. Evidence of intent: the
  user's request in this conversation, commit messages, `DECISIONS.md`/`CURRENT_STATE.md`, and the shape of the diff
  (a rename touching every call site, a new default value). When intent is unclear, ask the user; don't guess.
- Don't edit workflows to make them pass, don't add `continue-on-error`, and don't change application behaviour.
- Don't commit, push or stage. Use the project Python: `.venv/Scripts/python` (3.10, same as CI).

## 0. If you were given CI errors — triage them first

The arguments (or the user's message) may hold a CI failure log instead of, or as well as, options. Logs are long;
don't read or echo them whole. Accepted forms:

- **Pasted text** — work only from the lines that matter (below); never quote the log back.
- **A file path** (e.g. a saved `ci.log`) — don't `Read` it whole. `Grep` it for the patterns below, with a few lines
  of context, and read only around the hits.
- **A run id or Actions URL** — `gh run view <id> --log-failed` gives only the failed steps; pipe nothing else.
  Use `gh run list --branch <branch> -L 5` if the user says "the last run".

Extract, in this order of usefulness:

1. Job and step names that failed (`##[group]`, `Run ...`, `Process completed with exit code`).
2. pytest's `short test summary info` block: every `FAILED <test id> - <message>` and `ERROR <test id>` line.
3. For each failed test, the `E   ` lines and the last 2–3 traceback frames that point into `app/` or `tests/`.
   Collection errors (`ImportError`, `ModuleNotFoundError`, `fixture '...' not found`) matter most: one of them can
   fail a whole file.
4. Non-pytest failures: Ruff codes with `path:line`, `compileall` `SyntaxError`, pip-audit findings, detect-secrets
   hits, the `unexpected web packages` / `unexpected HTTP packages` checks, `apt`/`pip` install errors.

Skip everything else: setup/install output, passing tests, warnings summaries, timing lines, ANSI noise.

Write down a compact failure list (job → test id or check → one-line error) and use it as the work list: classify each
entry with the table in step 3 (also checking whether it's a CI-environment difference: Linux paths, xvfb timing,
PostgreSQL vs SQLite, missing optional package, the Tcl flake), reproduce only those ids locally in step 4, and still
do steps 1–2 for the changes they point at — a stale test usually has siblings the log didn't reach yet (pytest
stops a file at a collection error; CI cancels other jobs on a newer push).

## 1. Work out what changed

- Base: the argument if it's a ref, otherwise `git merge-base HEAD main`.
- Changes: `git diff --stat <base>` plus `git status --short` (uncommitted and untracked files count too).
- From the diff (`git diff <base> -- <file>`, per file, only the files that matter), list every **contract** that
  changed:
  - functions/classes/methods/modules renamed, moved, removed, or with a changed signature or return shape;
  - changed default values, constants, settings keys, enum values, storage/JSON/DB field names;
  - user-visible text, page or widget names that tests look up;
  - files or directories renamed, moved or deleted (workflows, packaging and scripts name paths);
  - new imports in `app/` (dependency boundaries are tested by CI);
  - edited `.github/workflows/*.yml` or `.github/scripts/*`.
- If a focus was given in the arguments, keep only the contracts in that area.

## 2. Find everything that still depends on the old contract

For each contract, `Grep` the **old** name/path/value (and the new one, to see what's already updated) in:

- `tests/` — direct calls, imports, `monkeypatch.setattr("module.attr")` strings, fixtures in `conftest.py`,
  expected strings and numbers, snapshot/golden files;
- `.github/workflows/`, `.github/scripts/`, `packaging/` (including `packaging/windows/build_windows.ps1`),
  `pyproject.toml` (pytest markers, paths), `requirements*.txt` and `requirements.lock`;
- docs that tests read (some tests check `docs/` or root docs for paths) — only if a test greps them.

Also check, without being asked by a failure:

- **Uncovered behaviour:** a new or changed public function/branch with no test exercising it → add a focused test
  next to the existing tests for that module. Mark it `@pytest.mark.slow` if it's slow (tiers come from
  `tests/test_tiers.py`).
- **Dependency boundaries** (CI's `desktop-only` and `direct-desktop` jobs): a new top-level import in `app/` of
  `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `psycopg`, `jwt`, `argon2` or `httpx` breaks
  desktop-only; `fastapi`/`starlette`/`uvicorn`/`httpx`/`jwt` in direct mode breaks direct-desktop. A new third-party
  package must be in `requirements.txt`, `requirements.lock` and the matching `requirements-*.txt`.
- **Workflow paths:** every `tests/...`, `packaging/...`, `.github/scripts/...` and `requirements*.txt` a workflow
  names must still exist.

## 3. Classify and act

| Finding | Action |
|---|---|
| Test expects the old contract, and the change was intended | Update it to the new contract, equally strict |
| Test expects the old contract, intent unclear | Ask the user before touching it |
| Test fails on behaviour that wasn't meant to change | Regression: report it, leave the test alone |
| Changed behaviour has no test | Add a focused test |
| Workflow/packaging/requirements names a moved or removed path or package | Update the reference to the new one (never drop the step) |
| Test is obsolete because the feature was deliberately removed | Remove only if the user's request removed the feature; say so in the report |

## 4. Run it like CI

Run in this order, stopping to fix stale tests between steps; rerun only what failed while debugging.

1. **Affected tests** — every test file you touched or that references a changed contract, without the `-m dev`
   filter (CI runs all tiers): `.venv/Scripts/python -m pytest <files> -q`.
2. **Workflow tests** — if anything under `.github/` or any path a workflow names changed:
   `.venv/Scripts/python -m pytest tests/test_workflows.py tests/test_release_workflow.py tests/test_deploy_workflow.py -q`.
3. **CI `test` job** (skip with `quick`) — `.venv/Scripts/python -m pytest` (whole suite; run it in the background,
   it takes several minutes and opens real windows), then `.venv/Scripts/python -m compileall -q .` and
   `.venv/Scripts/python -m ruff check .`.
4. **Jobs you can't fully reproduce here** — check statically instead and say so in the report:
   - `desktop-only`: the import check in step 2; optionally
     `.venv/Scripts/python -m pytest --ignore=tests/backend --ignore=tests/sync --ignore=tests/web --ignore=tests/direct`
     (same tests, but this venv has the server packages, so it can't prove their absence).
   - `postgres` / `direct-desktop`: only if backend, sync or direct code changed and the user agrees to start the
     disposable database (`docker compose -f docker-compose.postgres-test.yml up -d`, see the
     `schedule-maxing-debugging` skill). Never point tests at a real database.
   - `security-scan`: flag new secret-looking strings or changed pins in `requirements.lock`.
   - CI runs on Linux under xvfb: flag Windows-only assumptions in new tests (path separators, `\r\n`, `%LOCALAPPDATA%`).

A known flake: `TclError: invalid command name "tcl_findLibrary"` in a real-window test. Rerun that test alone; if it
passes, it's the flake, not the change.

## 5. Report

Keep it short:

- **From the CI log** (if one was given): each failure → stale test / regression / CI-environment issue, and what
  was done about it.
- **Updated (stale) tests:** `test id` — old expectation → new expectation, and why it was intended.
- **Added tests:** what behaviour they cover.
- **Regressions found:** failing `test id`, what broke, which change caused it (not fixed unless asked).
- **Needs your decision:** tests where intent was unclear.
- **CI jobs:** each job → run locally (pass/fail) / checked statically / not reproducible here, and why.
