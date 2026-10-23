---
name: ci-check
description: One pass that makes CI agree with the intended code. Collects every CI failure at once (all jobs of the latest run through gh, or pasted logs), reads ci_check_notes.md for the deliberate contract changes and the known CI-vs-Windows differences, predicts the failures CI has not shown yet (stale references, leftover threads, real-window and window-manager behaviour, timing, ordering, Linux paths, dependency boundaries, newer package releases), fixes stale tests, clear regressions and CI-environment gaps together, runs the local equivalent of every job, and records what it learned in ci_check_notes.md. Never commits or pushes.
argument-hint: "[optional: base ref, 'quick', a focus, pasted CI logs (all failed jobs at once), a log file path, or a GitHub Actions run id/URL]"
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
  - Bash(.venv/Scripts/python -m pip list:*)
  - Bash(.venv/Scripts/python -m pip install --dry-run:*)
---

# /ci-check: one pass, every job, nothing left for the next run

Arguments (may be empty): $ARGUMENTS

A CI failure must mean "the code is wrong". It must not mean "a test still expects something that was changed on
purpose", and it must not mean "CI is a different machine". **Success is one run:** after `/ci-check`, the next
push is green, or what is left is listed with the reason it could not be settled here. So don't stop at the
failures you were shown. CI shows one job at a time, cancels older runs when you push again, and stops a file at
its first collection error. Find the rest yourself.

## Rules (non-negotiable)

- **Never weaken a test to get green.** Never delete a meaningful assertion, loosen `==` to `in`/`>=`, add
  `skip`/`xfail`, widen a tolerance, catch an exception that used to propagate, or drop or narrow a test in a
  workflow. Updating a stale test means a new name, signature, path, value or label, with the test just as strict.
- **What a test may change without weakening it:**
  - nondeterminism that lives in the test itself, such as a frozen clock plus random ids, a fixed sleep, or
    unsorted filesystem order. Keep every check of *what* is there; drop only an order the code never promised,
    and say so in a comment;
  - the environment a test needs: a fixture (the `window_manager` fixture), or a system package or environment
    variable in the workflow's install step. Explain it in a workflow comment.
- **Fix clear regressions in the same run.** The cause must be certain (reproduced, or proven with a small script),
  the fix local, and the intended behaviour documented (a test, the notes, DECISIONS.md, a docstring). Add a test
  that fails without the fix: check it by running the test with the fix stashed. If the fix would change behaviour
  someone may have meant, or would reach across subsystems, report it and ask instead.
- **Intent decides, not the diff.** A test is stale only when the change was deliberate. Evidence: a Pending entry
  in `ci_check_notes.md`, the user's request, commit messages, DECISIONS.md/CURRENT_STATE.md, and the shape of the
  diff (a rename touching every call site). When intent is still unclear, ask; don't guess.
- Never add `continue-on-error`, change what a job runs, or point tests at a real database.
- Don't commit, push or stage. Use the project Python: `.venv/Scripts/python` (3.10, same as CI).

## 1. Load the memory (always first)

Read `ci_check_notes.md` at the repository root:

- **Pending** entries are deliberate contract changes not yet checked against CI. They join the contract list in
  step 3, already marked as intended.
- **CI environment facts** are how CI differs from this machine. Use them while classifying (step 5) and
  predicting (step 4).
- **Open CI items** may be what the log shows. Check them first.

## 2. Collect every failure, from every job, at once

Start the full suite now, in the background, unless the argument is `quick`: `.venv/Scripts/python -m pytest -q
-p no:cacheprovider`. It takes several minutes; analyse while it runs.

Then get the CI evidence:

- **`gh` available** (`gh --version`; if it isn't on PATH, try `C:\Program Files\GitHub CLI\gh.exe`):
  1. `gh run list --branch <branch> -L 5 --json databaseId,headSha,status,conclusion,createdAt`. Take the newest
     run for HEAD's SHA, or the newest completed run if HEAD has none.
  2. `gh run view <id> --json jobs --jq ".jobs[] | [.name, .conclusion] | @tsv"`: every job and its result.
     A `cancelled` or `skipped` job is unknown, not passing.
  3. `gh run view <id> --log-failed`: only the failed steps of all failed jobs. Never fetch the full log.
- **`gh` missing:** use what was pasted or given as a file, and say once that `winget install GitHub.cli` then
  `gh auth login` lets the next run fetch every job itself. Don't stop. Steps 3–4 still find the rest.
- **A log file:** `Grep` it for the patterns below with a little context; never `Read` it whole.

Extract only:

1. failed job and step names (`##[group]`, `Run ...`, `Process completed with exit code`);
2. every `FAILED <test id> - <message>` and `ERROR <test id>` line from pytest's short test summary. Collection
   errors (`ImportError`, `ModuleNotFoundError`, `fixture '...' not found`) first: one hides a whole file;
3. per failed test, the `E   ` lines and the last 2–3 frames that point into `app/` or `tests/`;
4. non-pytest failures: Ruff `path:line` codes, `compileall` errors, pip-audit findings, detect-secrets hits, the
   `unexpected web packages` / `unexpected HTTP packages` checks, apt/pip install errors;
5. warnings raised by **this repository's** code or tests (the warnings summary entries under `tests/` or `app/`).
   Third-party-only warnings go in the report, not the work list.

Write a compact failure list: job → test id or check → one-line error. Never quote the log back.

## 3. Work out what changed

- Base: the argument if it's a ref, otherwise `git merge-base HEAD main`.
- Changes: `git diff --stat <base>` plus `git status --short` (uncommitted and untracked files count too).
- List every **contract** that changed. Start with the Pending entries; then read the diff per file (only the
  files that matter) for anything they miss:
  - functions/classes/methods/modules renamed, moved, removed, or with a new signature or return shape;
  - changed defaults, constants, settings keys, environment variables, enum values, storage/JSON/DB fields;
  - user-visible text, page or widget names that tests look up;
  - files or directories renamed, moved or deleted;
  - new imports in `app/` (dependency boundaries);
  - new background threads, processes, executors, timers or `after()` loops, and changes to how they stop;
  - new real-window behaviour or Tk event bindings;
  - edited `.github/workflows/*.yml`, `.github/scripts/*` or `packaging/`.
- A contract the diff shows but the notes don't is still a contract. Add it to Pending now (step 7 settles it).
- If a focus was given, keep only the contracts in that area.

## 4. Predict what CI will fail on next

For each contract, `Grep` the **old** name, path or value (and the new one, to see what's already updated) in
`tests/` (calls, imports, `monkeypatch.setattr("module.attr")` strings, `conftest.py` fixtures, expected strings
and numbers, golden files), `.github/workflows/`, `.github/scripts/`, `packaging/` (including
`packaging/windows/build_windows.ps1`), `pyproject.toml`, `requirements*.txt`, `requirements.lock`, and the docs a
test reads.

Then sweep the changed code **and** the tests that exercise it for the failure classes this repository has hit on
CI (more in the notes' environment facts):

| Look for | Why CI fails | Check |
|---|---|---|
| `Thread(`, `ProcessPoolExecutor`, `ThreadPoolExecutor`, `multiprocessing`, `shutdown(wait=False)`, `daemon=True`, a new `after(` loop | still running after `AppServices.close()` → `test_desktop_isolation.py` | the close path joins or cancels it, also when close comes right after start |
| `iconify`, `deiconify`, `state()`, `wm_state`, `focus_force`, `focus_set`, `<Map>`, `<Unmap>`, `<FocusIn>`, `<Configure>`, `geometry`, `winfo_root*`, `overrideredirect` in tests | no window manager under `xvfb-run` | the test requests the `window_manager` fixture |
| a fixed clock with `uuid4` ids and an order assertion; `set` iteration; a query without `ORDER BY`; `os.listdir`, `glob`, `iterdir` | random order (filesystem order is unsorted on Linux) | sort, or order by something unique |
| `timeout=` below a few seconds, `time.sleep`, wall-clock deltas, `after(N)` racing another `after(M)` | Linux and xvfb timing differs | wait on a condition; reason about which callback runs first |
| `\\` in paths, `\r\n`, `%LOCALAPPDATA%`, `os.startfile`, `winreg`, `msvcrt`, a file name that differs only in case | Windows-only | `Path`, `os.linesep`, `config/settings.py` data paths |
| a font size or pixel width, DPI scaling | Linux Tk fonts and metrics differ | assert relationships, not pixels |
| a new third-party import, or one that crosses the desktop/web boundary | `desktop-only` (no `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `psycopg`, `jwt`, `argon2`, `httpx`) and `direct-desktop` (no `fastapi`, `starlette`, `uvicorn`, `httpx`, `jwt`) | top-level imports in `app/`; the package is in `requirements.txt`, `requirements.lock` and the matching `requirements-*.txt` |
| a `tests/...`, `packaging/...`, `.github/scripts/...` or `requirements*.txt` path a workflow names | moved or removed | the path exists |
| any changed file that has entries in `secret-scan-baseline.json` | lines added or removed above a baselined value make `security-scan` exit 123 | fix the moved `line_number`s by hand; check with the hook against a scratchpad copy of the baseline (notes: "Moving a baselined line") |
| a failure that looks like a library behaving differently | CI resolves `requirements*.txt` ranges to the newest release | `.venv/Scripts/python -m pip install --dry-run --ignore-installed -q -r <file> --report <scratchpad>/ci-resolve.json`, compared with `pip list`; cap the range or adapt the code |
| a new or changed public function or branch without a test | uncovered behaviour | add a focused test (`@pytest.mark.slow` if slow; tiers come from `tests/test_tiers.py`) |

Every hit is a finding, whether or not CI has reported it yet.

## 5. Classify and act, on everything at once

| Finding | Action |
|---|---|
| Test expects the old contract; the change was intended | Update it to the new contract, equally strict |
| Test expects the old contract; intent unclear | Ask the user; keep working on the rest |
| Clear regression with a certain cause and a local fix | Fix it, add a test that fails without the fix, record it in the notes |
| Regression whose fix changes intended behaviour or spans subsystems | Report it with the test id and the cause; ask |
| CI-environment difference (no WM, timing, Linux paths, packages) | Give the test what it needs (fixture, workflow install step, sorting, waiting on a condition); add the fact to the notes if it is new |
| Nondeterminism inside the test | Make it deterministic without dropping what it checks |
| Changed behaviour with no test | Add a focused test |
| A workflow, packaging or requirements file names a moved or removed path or package | Point it at the new one; never drop the step |
| Test obsolete because the feature was deliberately removed | Remove it only if the user's request removed the feature; say so |
| A warning from this repository's own code or tests | Fix the cause; when the warning is the point of the test, assert it with `pytest.warns` |

Prove a cause before you fix it. Reproduce it locally, or write a small script in the scratchpad that shows it
(for example, the threads left after `close()`). When the cause only appears on Linux, explain the mechanism in
the report.

## 6. Run it like CI

Run in this order; rerun only what failed while debugging.

1. **Affected tests:** every test file you touched or that names a changed contract, without `-m dev` (CI runs all
   tiers): `.venv/Scripts/python -m pytest <files> -q -p no:cacheprovider`. Run new or changed tests **three
   times**, to catch order and timing flakes.
2. **Isolation:** if anything starts or stops threads, processes or timers: `.venv/Scripts/python -m pytest
   tests/test_desktop_isolation.py -q`, three times.
3. **Workflow tests:** if anything under `.github/` or a path a workflow names changed:
   `.venv/Scripts/python -m pytest tests/test_workflows.py tests/test_release_workflow.py tests/test_deploy_workflow.py -q`.
4. **The `test` job** (skip with `quick`): the background full suite from step 2. Rerun only its failures after your
   fixes. Then `.venv/Scripts/python -m compileall -q .` and `.venv/Scripts/python -m ruff check .`. A failure in a
   file you didn't touch is still yours to classify: an existing flake (prove it), or a real one.
5. **Jobs you can't reproduce here.** Check them statically and say so:
   - `desktop-only` / `direct-desktop`: the boundary rows of step 4 (this venv has every package, so it can't prove
     their absence);
   - `postgres` / `direct-desktop` on PostgreSQL: only if backend, sync or direct code changed and the user agrees
     to start the disposable database (`docker compose -f docker-compose.postgres-test.yml up -d`; see the
     `schedule-maxing-debugging` skill);
   - `security-scan`: new secret-looking strings, and changed pins in `requirements.lock`;
   - anything that only Linux shows (a WM fixture, a Linux-only race): mark it "needs the next CI run".

## 7. Update the memory

Edit `ci_check_notes.md`. Keep its structure, and keep entries short:

- Move each Pending entry you settled to **Checked**, with an **Outcome** line: stale test updated / regression
  fixed (commit or "uncommitted") / CI environment / needs decision. Keep the latest ten Checked entries.
- Add any new **CI environment fact** you proved, so the next run predicts it instead of discovering it.
- Update **Open CI items**: add what is unverified or waiting on the owner; remove what this run settled.

## 8. Report

Keep it short:

- **CI evidence:** where it came from (gh run id with each job's result, or pasted logs), and which jobs are
  unknown.
- **Fixed:** each finding → stale test / regression / CI environment / flaky test, with the test id or check, the
  cause in one line, and what changed. Mark the ones CI had not reported yet as *predicted*.
- **Added tests:** what they cover; for a regression fix, that the test failed without the fix.
- **Needs your decision:** unclear intent, behaviour changes, new dependencies.
- **CI jobs:** each job → passed locally / checked statically / needs the next CI run, and why.
- **Notes updated:** what moved to Checked and which facts or open items were added.
