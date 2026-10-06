# Prompt 6 — Update check and installer hand-off

Implement only this prompt, then stop with a completion report. Prerequisites: Prompts 1, 2, 4 and 5. Read `CLAUDE.md`,
`docs/milestone-9-prompts/README.md` and `00-audit-and-architecture.md` (sections 3, 5), inspect `git status` and the
code below before editing. Do not commit, push or read `.env`. This is security-sensitive code: prefer refusing an
update over installing a doubtful one.

Objective: an installed Schedule Maxing checks GitHub Releases in the background after the window is up, offers a newer
stable version, and on the user's approval downloads the installer, verifies it, runs it and exits — with the data in
`%LOCALAPPDATA%\ScheduleMaxing` untouched and the current version still usable if anything fails.

Audit decisions applied: download and run the newer Inno Setup installer (decision 2); GitHub `releases/latest` plus
`SHA256SUMS.txt`, GitHub hosts only (decision 3); `app/version.py` is the installed version (decision 4).

Inspect first: `app/version.py`, `app/runtime.py` (`updates_dir`, `is_frozen`), `app/desktop.py` (the update seam),
`app/sync/transport.py` (the standard-library HTTP and error-mapping style to follow), `app/ui/background.py`
(`run_in_background`, `WorkerRegistry`), `app/ui/ui_settings.py`, `app/ui/pages.py` (`SettingsPage`), `app/ui/shell.py`
(status bar), `app/app.py` (`_on_close`, `close_services`), `packaging/windows/ScheduleMaxing.iss` (silent switches and
the relaunch entry), `.github/workflows/release.yml` (asset names), `tests/test_desktop_isolation.py`.

Design:

1. `app/update/versioning.py` — parse `MAJOR.MINOR.PATCH` (optional leading `v`) into an integer tuple and compare
   numerically. Anything else, including pre-release or build suffixes, is "not a stable version" and is never offered.
   No string comparison. No new dependency.
2. `app/update/release_feed.py` — `GET https://api.github.com/repos/<owner>/<repo>/releases/latest` with a short
   timeout, a `User-Agent` naming the app and version, and a response size cap. The repository slug is one constant in
   `config/settings.py` (overridable by an environment variable for tests and forks, "off" disables). Validate the
   payload defensively: tag is a stable version; `draft` and `prerelease` are false; exactly one asset is named
   `ScheduleMaxing-Setup-<that version>.exe`; a `SHA256SUMS.txt` asset exists; both download URLs are HTTPS on the
   expected repository path. Malformed or unexpected data yields "no update", logged, never an exception into the UI.
3. `app/update/downloader.py` — download into `runtime.updates_dir()` under a temporary name:
   - HTTPS only; every redirect target must be on an allow-list of GitHub hosts (`github.com`,
     `objects.githubusercontent.com`, `release-assets.githubusercontent.com`; keep the list in one place);
   - enforce the size announced by the API and a hard upper bound; stream to disk; support cancellation;
   - fetch `SHA256SUMS.txt`, find the line for the exact installer filename, compare against the SHA-256 of the
     downloaded file (constant-time compare); on mismatch, truncation or any error delete the partial file and report
     failure;
   - only after verification rename to the final filename. Remove stale downloads on the next check.
   - Leave a clearly named seam for Authenticode verification, documented as required once signing exists.
4. `app/update/service.py` — Tk-free orchestration: `check()` returns "up to date", "update available (version, notes
   URL, size)" or "unavailable (reason)"; `download()`; `launch_installer()` which starts the verified installer with
   the documented silent/relaunch switches as a detached process and returns so the app can close. It refuses to run
   anything that was not produced by `download()` in this process (path inside `updates_dir`, recorded hash re-checked
   immediately before launch). It never offers a version lower than or equal to the installed one. From source
   (`not is_frozen()`), checking works but installing is disabled with an explanatory message.
5. Preferences: `check_for_updates` (default on) and `skipped_version` in `app/ui/ui_settings.py`, tolerant of old
   files. A Settings row "Automatically check for updates" plus a "Check now" button. Structure the code so a channel
   setting can be added later; implement only the stable channel.
6. UI (`app/ui/update_controller.py` plus minimal widgets): the automatic check starts a few seconds after the window
   is shown, through `run_in_background`, at most once per start and not more than once per day (store the last check
   time). A newer version produces a non-blocking notice with "Update now", "Later" and "Skip this version". "Update
   now" shows progress and can be cancelled; on success the app explains that it will close, closes through the normal
   shutdown path (`_on_close`, so the database is closed cleanly), and the installer takes over and relaunches. A
   failure at any step leaves the app running with a short message and a log entry. A manual "Check now" reports "You
   are up to date" or the failure reason; the automatic check stays silent on failure and when up to date.
7. Per-machine installs show a UAC prompt from the installer; say so in the confirmation text. If the user declines
   UAC, the installed version is untouched.
8. Database across versions: updates never touch the data directory. Confirm the Prompt 2 backup-then-migrate path is
   what runs on first start of the new version, and add the cross-version test below.
9. `docs/windows-distribution.md`, section "Updates": the flow, the trust model (HTTPS + GitHub hosts + checksum =
   integrity, not authorship, until signing), how to disable checks, how withdrawing a release stops propagation, and
   that the updater requires the releases to be publicly readable — no token is ever embedded.

Constraints: standard library only for networking and hashing; no network access on the Tk thread; no check at import
time; a failed or slow check never delays start-up or shutdown; `tests/test_desktop_isolation.py` must still pass (with
the backend "off" and sockets blocked the app starts and leaves no thread behind — make the update check respect the
same conditions and be disabled in that probe's environment by its documented switch).

Tests (all with an injected fake transport — no real network; `dev` tier unless a real window is needed):

- no update available (same version); newer version available; older version published (never offered);
- malformed version metadata, missing fields, wrong asset name, extra matching assets, non-HTTPS or foreign-host URLs;
- pre-release and draft releases ignored by stable users; development/suffixed versions ignored;
- GitHub unavailable; HTTP 403/404/5xx; rate limited; timeout — each is "unavailable", none raises into the caller;
- version comparison: `1.4.0 > 1.3.2`, `1.10.0 > 1.9.9`, `v` prefix, equal versions;
- corrupted download (truncated), size mismatch, checksum mismatch, missing checksum line, redirect to a foreign host —
  the partial file is removed and nothing is launched;
- successful download: verified file at the expected path; the launch command is exactly the documented one (assert
  on an injected process launcher; never execute a real installer in tests);
- the launcher refuses a path outside the updates directory and a file whose hash changed after verification;
- user declines; user skips a version and is not offered it again, but is offered a later one;
- the current version is not offered repeatedly; the once-per-day limit; the setting off means no automatic request;
- the check does not block the GUI: with a transport that blocks on an event, the call that schedules the check
  returns immediately and the result arrives through the background registry;
- app data intact: a data directory with a database, settings and a model file is byte-for-byte unchanged after a
  check, a download and a simulated launch;
- database migration across application versions: a database created at an older schema version opens with the
  current code, is backed up first, migrates to the latest version and keeps its rows.

Verification: focused tests, then once `python -m pytest -m dev` and `python -m ruff check .`. A real end-to-end update
needs two published releases and cannot be run here; describe the manual test for Prompt 7 and say it is untested.

Completion report: files changed; the trust model and its limits; settings added; the manual end-to-end update test;
what the owner must do (make releases publicly readable, publish two versions to test); what is untested.
