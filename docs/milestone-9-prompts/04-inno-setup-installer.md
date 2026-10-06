# Prompt 4 — Inno Setup installer, metadata and upgrade behaviour

Implement only this prompt, then stop with a completion report. Prerequisite: Prompt 3 (a working
`dist/ScheduleMaxing/`). Read `CLAUDE.md`, `docs/milestone-9-prompts/README.md` and `00-audit-and-architecture.md`
(sections 2, 3, 5), inspect `git status` and `packaging/windows/` before editing. Do not commit, push or read `.env`.

Objective: `ScheduleMaxing-Setup-<version>.exe`, a normal 64-bit Windows installer that installs, upgrades in place and
uninstalls cleanly, and never touches the user's data in `%LOCALAPPDATA%\ScheduleMaxing`.

Audit decisions applied: per-machine install under Program Files by default with the per-user choice allowed
(decision 1); the version comes from `app/version.py` only (decision 4); neutral publisher (section 5).

Inspect first: `packaging/windows/ScheduleMaxing.spec`, `build_windows.ps1`, `version_info.py`, `app/desktop.py` (the
single-instance mutex name from Prompt 2), `app/version.py`, `docs/windows-distribution.md`.

Required work:

1. `packaging/windows/ScheduleMaxing.iss` (Inno Setup 6, commented):
   - `AppId` a fixed GUID generated once and never changed (it is what makes upgrades and Add/Remove Programs work);
   - `AppName=Schedule Maxing`; `AppVersion`, `VersionInfoVersion` and the output name passed in from the build script
     (`/DAppVersion=...`), never hard-coded; `AppPublisher` from one define with a neutral default;
   - `ArchitecturesAllowed=x64compatible`, `ArchitecturesInstallIn64BitMode=x64compatible`; `MinVersion=10.0`;
   - `DefaultDirName={autopf}\Schedule Maxing`; `PrivilegesRequired=admin` with
     `PrivilegesRequiredOverridesAllowed=dialog`; `DisableProgramGroupPage=yes`;
   - `[Files]` the whole `dist\ScheduleMaxing\*` tree, `recursesubdirs`, `ignoreversion`;
   - `[InstallDelete]` remove the previous `{app}\_internal` before copying, so files dropped by a newer build do not
     linger after an upgrade;
   - Start Menu shortcut; optional desktop shortcut as an unchecked task; installer and uninstall icons;
   - `AppMutex` set to the application's mutex name, `CloseApplications=yes`, so an upgrade asks to close a running app;
   - `[Run]` optional "Launch Schedule Maxing" after install (`postinstall skipifsilent`), and a launch entry that also
     works after a silent update so the updater in Prompt 6 can restart the app (document the exact switch);
   - uninstall removes only what the installer put in `{app}`. No `[UninstallDelete]` entry and no code may touch
     `{localappdata}\ScheduleMaxing` or Credential Manager. State this in a comment at the top of the file;
   - `SetupIconFile`, `UninstallDisplayIcon`, `UninstallDisplayName`, `WizardStyle=modern`, LZMA2 compression,
     `OutputBaseFilename=ScheduleMaxing-Setup-{#AppVersion}`.
2. Extend `build_windows.ps1`: locate `ISCC.exe` (PATH, the default install folders, or an `-InnoSetupPath` parameter),
   compile the installer with the version from `app/version.py`, write `SHA256SUMS.txt` next to it
   (`<hash>  <filename>` format, lowercase hex), and print both paths. A missing ISCC is a clear error that says how to
   install it, unless the installer step was skipped.
3. Signing hook: one function in the build script that signs a file with `signtool` when signing parameters are
   supplied (certificate thumbprint or PFX path from parameters/environment, timestamp URL), and is a logged no-op
   otherwise. Call it for `ScheduleMaxing.exe` before the installer is compiled and for the installer afterwards; pass
   a `SignTool` definition to Inno Setup only when signing is configured so the uninstaller is signed too. Never store a
   certificate or password in the repository.
4. A user-data removal path that is explicit and separate from uninstall: document in `docs/windows-distribution.md`
   where the data lives and how to delete it by hand. Do not add an automatic or default-on deletion.
5. `docs/windows-distribution.md`: install, silent install switches, upgrade, uninstall, what persists and why, the
   signing parameters, and the manual installer test script below.

Constraints: no custom file-replacement logic; no registry writes beyond what Inno Setup does for uninstall
information; the installer contains nothing that the Prompt 3 bundle checks forbid.

Tests to add (`dev` tier, no ISCC required): extend `tests/test_packaging_config.py` — the `.iss` has a fixed `AppId`,
no hard-coded version, 64-bit directives, the mutex name equal to the constant used by `app/desktop.py`, the launch
entry, and no reference to `{localappdata}` or `{userappdata}` in any delete section; the checksum writer produces a
verifiable `SHA256SUMS.txt` for a sample file.

Verification — do what this machine allows and say exactly what was done:

- If Inno Setup is installed (or the owner approves installing it), build the installer and run a silent install to a
  scratch directory containing spaces, launch the installed executable's self-test, upgrade over it with a second build
  whose version is higher, confirm the data directory used by the self-test is intact, then uninstall silently and
  confirm the data directory still exists and `{app}` is gone.
- If it is not installed, say so; do not claim the installer works.
- Then once `python -m pytest -m dev` and `python -m ruff check .`.

Manual test script to include in the docs (the owner runs it; it cannot be automated here): interactive install to
`C:\Program Files\Schedule Maxing`; Start Menu and desktop shortcuts and icons; Add/Remove Programs entry with name,
version and publisher; first launch creates `%LOCALAPPDATA%\ScheduleMaxing`; add data; upgrade with a newer installer
while the app is running (close prompt appears); data present after upgrade; uninstall; data still present; reinstall
sees the data.

Completion report: files changed; build command and outputs; what was run here versus left manual; the `AppId`;
actions the owner must take (install Inno Setup 6, choose the publisher name, obtain a certificate later).
