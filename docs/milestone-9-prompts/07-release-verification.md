# Prompt 7 — Production-release audit for 1.0.0

This prompt adds no features. It audits everything Prompts 1–6 produced, fixes release-blocking defects, and ends with
the checklist of what the owner must still do by hand. Read `CLAUDE.md`, `docs/milestone-9-prompts/README.md`,
`00-audit-and-architecture.md` and the completion reports of Prompts 1–6; inspect `git status` and the current code. The
checkout is the source of truth. Do not commit, push, tag, publish or read `.env`.

Question to answer honestly: can a Windows 10 or 11 user with no Python go from `ScheduleMaxing-Setup-1.0.0.exe` to an
installed app, launch it, use it, close it, reopen it and find their data — and later update it without losing anything?

Part A — Re-audit against the original requirements (read the code, do not trust earlier reports):

1. Entry point: exactly one production entry (`app/desktop.py`); development entries still work; no obsolete or
   competing packaging, startup or configuration code was left behind.
2. Paths: search the application for `__file__`, `os.getcwd`, relative `open(...)`, `Path("...")` literals and
   `sys.frozen`. Every bundled resource goes through `app/runtime.py`; every writer resolves under the data directory,
   a user-chosen path or the temp directory; nothing writes beside the executable.
3. Version: one definition; the About page, exe metadata, installer and updater all read it.
4. Secrets: the bundle contains no `.env`, `DATABASE_URL`, `JWT_SECRET`, server code or database driver; the frozen app
   cannot enter direct PostgreSQL mode; logs contain no credentials (grep a real log produced during this audit).
5. Persistence: first launch creates the directory and database; later launches reuse it; an older database is backed
   up then migrated; a failed migration or backup leaves the database untouched; a newer database is refused with a
   clear message; reinstall and upgrade never overwrite data; uninstall leaves it.
6. Offline: no network on the Tk thread; unreachable or sleeping backend, failed login, sync failure and timeouts all
   leave the app usable; the update check is silent on failure.
7. Logging and crashes: a log exists at the documented location with useful start-up, migration, sync and update
   lines; an unhandled exception produces a log entry and a readable dialog.
8. Packaging: spec excludes and data are correct; the bundle checks pass; the icon is embedded and shown.
9. Installer: fixed `AppId`, 64-bit, shortcuts, Add/Remove Programs entry, upgrade in place, no deletion of user data.
10. Release workflow: tag/version check, tests gate the build, the build gates publishing, least privilege, no secrets
    in the file.
11. Updater: stable releases only, numeric version comparison, GitHub hosts only, checksum verified before launch,
    nothing launched on any failure, user approval required.

Part B — Execute what this machine can:

- `python -m ruff check .`, `python -m compileall .`, and the test suites: `python -m pytest -m dev` once; the full
  `python -m pytest` once only if the owner has not asked for minimal testing in this session.
- `packaging/windows/build_windows.ps1` end to end, the bundle checks and the packaged smoke test.
- If Inno Setup is available: silent install to a path with spaces, self-test, relaunch against the same data,
  upgrade with a higher-versioned build, verify data, uninstall, verify data remains.
- The packaged app started with the backend unreachable, with a corrupt `ui_settings.json`, and against a copy of an
  older-schema database.

Fix every release-blocking defect found, with a focused test for each fix, and re-run only the affected checks. Do not
delete or weaken tests. Record non-blocking issues instead of fixing them.

Part C — Final report, in this order:

1. Verdict: ready, ready with listed conditions, or not ready — and why.
2. A table of every requirement in Parts A and B with one of: verified automatically (command), verified manually here
   (what was done), not verified (why).
3. Defects fixed in this prompt, with files.
4. Known limitations and non-blocking issues.
5. The manual checklist before publishing 1.0.0 — things only the owner can do:
   - replace the placeholder icon; choose the publisher name;
   - install Inno Setup 6 locally if building locally;
   - confirm releases are publicly readable (the updater needs it) and configure repository Actions permissions;
   - run the release workflow once through `workflow_dispatch` and inspect the artifacts;
   - test the installer on a clean Windows 10 x64 machine and a clean Windows 11 x64 machine (a fresh VM or Windows
     Sandbox with no Python): install to Program Files, shortcuts, first launch, every page, create and schedule tasks
     with each mode, CSV import/export, sign in, sync, sign out, offline launch, restart with data present, upgrade
     from 1.0.0 to a test 1.0.1 through the in-app updater, uninstall with data retained;
   - note SmartScreen/antivirus behaviour for the unsigned build and decide on a code-signing certificate;
   - verify the Render backend is reachable from the packaged app and its cold-start delay is tolerable;
   - commit, merge, bump the version if needed, tag `v1.0.0`, push the tag, check the published release and its
     checksums;
   - only claim support for the Windows versions actually tested.
6. Exact commands for the release.
