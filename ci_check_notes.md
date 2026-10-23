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

(none)

## Checked

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
- **Known Tcl flake:** `TclError: invalid command name "tcl_findLibrary"` in a real-window test. Rerun the test
  alone; if it passes, it was the flake.

## Open CI items

- Starlette warns "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead". Waiting on
  the owner: adding `httpx2` means a new dependency in `requirements.txt` and `requirements.lock`.
- `gh` is installed and logged in, but a shell opened before the install has the old PATH. If `gh` is not found,
  call `C:\Program Files\GitHub CLI\gh.exe` directly.
