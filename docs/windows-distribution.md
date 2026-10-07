# Windows distribution: build, install, release and update

How Schedule Maxing becomes `ScheduleMaxing-Setup-<version>.exe`, what that installer does on a user's computer, how a
release is published, and how installed copies update themselves. The audit behind this design is
[milestone-9-prompts/00-audit-and-architecture.md](milestone-9-prompts/00-audit-and-architecture.md).

Supported for 1.0.0: **64-bit Windows 10 and Windows 11**. Only claim a Windows version that the
[manual release checklist](#manual-release-checklist) was actually run on.

## What is where

| | Location | Written at run time? |
|---|---|---|
| Program | `C:\Program Files\Schedule Maxing\` (`ScheduleMaxing.exe`, `_internal\`, `unins000.exe`) | never |
| Bundled resources | `_internal\config\task_preference.yaml`, `_internal\assets\ScheduleMaxing.ico` | never |
| Database | `%LOCALAPPDATA%\ScheduleMaxing\executions.db` | yes |
| Settings | `%LOCALAPPDATA%\ScheduleMaxing\ui_settings.json`, `task_defaults.json` | yes |
| Duration model | `%LOCALAPPDATA%\ScheduleMaxing\ml_duration_model.joblib` (+ `.meta.json`) | yes |
| Logs | `%LOCALAPPDATA%\ScheduleMaxing\logs\schedule-maxing.log` (1 MB, 5 older files kept) | yes |
| Database backups | `%LOCALAPPDATA%\ScheduleMaxing\backups\executions-v<schema>-<time>.db` (newest 5) | before a migration |
| Update downloads | `%LOCALAPPDATA%\ScheduleMaxing\updates\` (at most one installer) | during an update |
| Saved sign-in | Windows Credential Manager, entry "Schedule Maxing" | when "Keep me signed in" is on |

`app/runtime.py` is the one module that knows whether the program runs from source or packaged. The packaged client
contains no server code, database driver or credential; it reaches PostgreSQL only through the HTTPS API
(`config/settings.py`, `DEFAULT_BACKEND_URL`). The direct PostgreSQL mode is a development tool of `python -m app.app`
and cannot be selected in the packaged program.

## Entry points

| Command | What it is |
|---|---|
| `ScheduleMaxing.exe` / `python -m app.desktop` | The production entry point: log file, error dialogs, local storage only, background update check |
| `python -m app.app` | Development: the same window with a console; also `--storage postgres` |
| `python -m app.main` | The command-line scheduler (not packaged) |
| `python -m app.web` | The local web service (not packaged) |

## Building

Prerequisites on the build computer: 64-bit Python 3.10+, and [Inno Setup 6](https://jrsoftware.org/isdl.php) for the
installer step. The user's computer needs none of this.

```powershell
packaging\windows\build_windows.ps1
```

| Step | What happens | Stops the build when |
|---|---|---|
| Build environment | `.venv-build` from `requirements-build.txt`: the desktop runtime and PyInstaller only | install fails, Python is 32-bit |
| Tests | `python -m pytest -m dev` in `.venv` (`-SkipTests` to skip) | any test fails |
| PyInstaller | `packaging/windows/ScheduleMaxing.spec` → `dist\ScheduleMaxing\` (a folder build, windowed, no UPX) | the build fails |
| Bundle check | `check_bundle.py`: no `.env`, server package, database driver, key or log; resources present | anything forbidden or missing |
| Smoke test | `smoke_test.py`: the packaged self-test, twice in one data folder and once from a path with spaces | any check fails |
| Installer | `ScheduleMaxing.iss` → `dist\installer\ScheduleMaxing-Setup-<version>.exe` (`-SkipInstaller` to skip) | Inno Setup is missing or fails |
| Checksums | `checksums.py` → `dist\installer\SHA256SUMS.txt` | — |
| Signing | the executable, the installer and its uninstaller, when configured | signing is configured but fails |

Other switches: `-Clean` (delete `build\`, `dist\` and `.venv-build` first), `-Python`, `-TestPython`, `-InnoSetupPath`.

The version is written once, in `app/version.py`. The executable's properties, the installer, the About page and the
updater read it from there.

**What the build leaves out, and why.** `backend/`, `app/web`, the direct PostgreSQL modules, SQLAlchemy, psycopg,
Alembic, FastAPI, PyJWT, argon2 and python-dotenv: a distributed client must not carry database credentials or the code
that uses them. Also tests, samples, benchmarks and the command-line tools. The build environment does not even install
those packages, the spec excludes them by name, and `check_bundle.py` fails the build if one appears.

**The packaged self-test.** `ScheduleMaxing.exe --self-test report.json` (`app/selftest.py`) is how a build proves it
works: bundled resources, the compiled dependencies (CustomTkinter, tzdata, pandas, scikit-learn, joblib, keyring's
Windows backend), database creation in the data folder, a schedule generated in every mode, an execution recorded, a
CSV export and re-import, a restart with the data still there, and the real window with every page built. It refuses
to run unless `SCHEDULE_MAXING_DATA_DIR` names a scratch folder, so it cannot touch real data.

Expected size: about 190 MB unpacked (pandas, NumPy, SciPy and scikit-learn are most of it). Start-up takes several
seconds because those libraries load at start.

The icon `assets/ScheduleMaxing.ico` is built from the artwork in `assets/icon/` (one PNG per size: 16, 24, 32, 48, 64,
128 and 256 px) by `python packaging/windows/make_icon.py`. Run that again after changing an image; the `.ico` is
committed, so a normal build does not need to.

## Installing, upgrading and uninstalling

The installer:

- installs for everyone under `C:\Program Files\Schedule Maxing` (one Windows permission prompt), or — chosen on its
  first page, or with `/CURRENTUSER` — only for the current user with no prompt;
- adds a Start Menu shortcut and, if ticked, a desktop shortcut;
- registers in **Settings > Apps** with its name, version, publisher and icon;
- offers to start the app when it finishes.

Running a newer installer **upgrades in place**: it waits for (or asks you to close) a running copy, replaces the
program's files completely, and keeps the same install folder and shortcuts.

**Your data is never touched** by installing, upgrading or uninstalling. Uninstalling removes the program folder and
shortcuts only. To remove the data as well — a separate, deliberate step — close the app and delete the folder
`%LOCALAPPDATA%\ScheduleMaxing`, and remove the "Schedule Maxing" entry in Windows Credential Manager if you used
"Keep me signed in".

Silent use:

```powershell
ScheduleMaxing-Setup-1.2.3.exe /SILENT /NORESTART              # progress window only
ScheduleMaxing-Setup-1.2.3.exe /VERYSILENT /SUPPRESSMSGBOXES   # nothing shown
ScheduleMaxing-Setup-1.2.3.exe /SILENT /NORESTART /RELAUNCH=1  # what the in-app updater runs: start the app afterwards
```

`packaging/windows/test_installer.py <installer>` checks all of this automatically in a scratch folder: install, first
launch, restart, upgrade, uninstall, with the data folder compared byte for byte. It runs in the release workflow.

### The database across versions

The new version migrates the database the first time it opens it (`app/execution/db.py`): each migration step is one
transaction with its schema-version bump, so a failed step leaves the previous version intact. Before any step runs,
the file is copied to `backups\` (`app/execution/backup.py`); if that copy cannot be made, nothing is migrated. A
database written by a *newer* version is refused untouched with a message to install the latest version.

To go back to a backup: close the app, copy the wanted `backups\executions-v<schema>-<time>.db` over `executions.db`,
and start the version of the app that matches that schema.

## Releasing

```powershell
# 1. set the new version in app/version.py, commit, merge to main
git tag v1.2.3
git push origin v1.2.3
```

`.github/workflows/release.yml` then runs, each job only if the one before passed:

1. **verify** — the tag must equal `v` + the version in `app/version.py`, and must not be released already;
2. **test** — the complete test suite, compileall and ruff, as CI runs them;
3. **build** (Windows) — packaging tests, `build_windows.ps1`, the install/upgrade/uninstall test, checksum verification;
4. **publish** — a GitHub Release (not a draft, not a pre-release) with exactly `ScheduleMaxing-Setup-1.2.3.exe` and
   `SHA256SUMS.txt`.

A **dry run** is the same workflow started by hand (Actions > Release > Run workflow): it builds and tests everything
and leaves the installer as a workflow artifact, but never publishes.

**Skipping the tests (off switch).** The `test` job can be switched off with a repository variable: Settings >
Secrets and variables > Actions > **Variables** > New repository variable, name `SKIP_RELEASE_TESTS`, value `true`.
The run then shows `test` as skipped and a warning "Untested build"; everything else (tag check, packaging tests,
build, smoke test, installer test) still runs. Any other value, or no variable, means the tests must pass. Delete the
variable as soon as that release is out: while it exists, every release is published without the test suite.

Repository settings the workflow needs: Actions allowed to create releases (Settings > Actions > General > Workflow
permissions is enough with the job's own `contents: write`). Optional: a tag protection rule for `v*`.

### Withdrawing a release

Installed copies read only GitHub's *latest stable release*. To stop a bad version from spreading, edit the release and
mark it **pre-release** (or delete it): it is no longer offered, at once. Then publish a higher version with the fix.
The app never installs a version that is not newer than the one installed, so people who already took the bad version
are repaired only by that higher version — there is no automatic downgrade.

### Code signing

Nothing needs a certificate to work, but without one Windows SmartScreen warns on download and on first run. The build
is ready for one: `build_windows.ps1` signs `ScheduleMaxing.exe`, the installer and its uninstaller when given a
certificate, and says "Not signed" otherwise.

| Where | Setting |
|---|---|
| Local build, certificate in the Windows store | `-SignThumbprint <sha1>` or `SM_SIGN_THUMBPRINT` |
| Local build, certificate file | `-SignPfx <file>` or `SM_SIGN_PFX`, password in `SM_SIGN_PFX_PASSWORD` |
| Release workflow | repository secrets `SM_SIGN_PFX_BASE64` (the `.pfx`, base64) and `SM_SIGN_PFX_PASSWORD` |

Never commit a certificate. When releases are signed, add the signature check to
`app/update/downloader.py: verify_signature` so the updater refuses an unsigned or foreign-signed installer.

## Updates

1. A few seconds after the window opens — never before, and never blocking it — the app asks GitHub for the latest
   stable release of `Ramtin-Mandom/Schedule_Maxing`. At most once a day, in a background thread.
2. If that version is newer than the installed one (compared as numbers) and was not skipped, a notice offers
   **Update now**, **Later** or **Skip this version**. Nothing is downloaded before "Update now".
3. The installer is downloaded to `updates\`, checked against the release's `SHA256SUMS.txt` and its announced size,
   and deleted if anything differs.
4. The verified installer is started, the app closes normally, the installer replaces the program and starts the new
   version. Windows asks for permission if the app is installed for everyone.

If GitHub is unreachable, the check fails quietly (one log line) and is tried again at the next start. Any failure
during an update leaves the installed version running and unchanged. **Settings > Updates** has "Automatically check
for updates" and "Check now".

| Variable | Effect |
|---|---|
| `SCHEDULE_MAXING_UPDATE_CHECK=off` | no update checks at all |
| `SCHEDULE_MAXING_UPDATE_REPOSITORY=owner/name` | another repository's releases (a fork) |

**Trust model.** The app contacts only `api.github.com`, `github.com` and GitHub's two download hosts, over HTTPS, and
refuses any other address or redirect. The release must be a stable `vX.Y.Z` tag with exactly one installer of the
expected name and one checksum file, both inside that release. The checksum protects against a corrupted, truncated or
swapped download. It does **not** prove who built the installer, because it is published alongside it; that is what
code signing adds. The releases must be publicly readable: no access token is, or may ever be, built into the app.

## Automated and manual verification

| Checked | How |
|---|---|
| Paths, version source, entry point, logging, error hooks, backup and migration, updater logic, packaging and workflow configuration | `python -m pytest -m dev` |
| The packaged program starts, finds its resources, creates and reopens its data, schedules, opens every page | `smoke_test.py` in every build |
| Nothing private is packaged | `check_bundle.py` in every build |
| Install, restart, upgrade and uninstall keep the data | `test_installer.py` in the release workflow |

### Manual release checklist

These cannot be automated from a developer machine. Do them on a **clean** Windows 10 x64 and a clean Windows 11 x64
(a fresh virtual machine or Windows Sandbox, with no Python installed):

- [ ] Download the installer with a browser; note what SmartScreen says for the unsigned build.
- [ ] Install to `C:\Program Files\Schedule Maxing`; Start Menu and desktop shortcuts show the icon.
- [ ] Settings > Apps lists Schedule Maxing with the right version and publisher.
- [ ] First launch opens the window; `%LOCALAPPDATA%\ScheduleMaxing\executions.db` and `logs\` appear.
- [ ] Visit every page; add tasks; generate a day in each scheduling mode; start and complete a task.
- [ ] Export a planning CSV and import it again.
- [ ] Create an account or sign in, synchronize, sign out. The first request may wait for the server to wake.
- [ ] Disconnect the network; restart the app; it opens and local work is saved. Reconnect; it synchronizes.
- [ ] Close and reopen: everything is still there.
- [ ] Publish a test release one patch higher; the app offers it; "Update now" installs it and reopens with the data.
- [ ] Uninstall; the data folder is still there; reinstall; the data is back.

## Known limitations

- Unsigned builds trigger SmartScreen and may be flagged by antivirus heuristics.
- The first start after installing is slow while Windows scans the new files.
- Only one copy of the app can use the data at a time; a second window shows a clear message.
- The updater needs the releases to be publicly readable.
