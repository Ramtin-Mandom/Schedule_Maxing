# Milestone 9 prompts — Windows distribution

Read `00-audit-and-architecture.md` first. Run the prompts in order, one per session; each leaves the repository
working. The checkout takes precedence over any file or line reference that has drifted.

| File | Scope |
|---|---|
| `01-runtime-paths-entry-version.md` | Version source, runtime helper, production entry point |
| `02-persistence-logging-safety.md` | Logging, exception hooks, migration backup, frozen guards |
| `03-pyinstaller-build.md` | PyInstaller spec, icon, build script, smoke test |
| `04-inno-setup-installer.md` | Inno Setup installer, metadata, upgrade/uninstall |
| `05-release-workflow.md` | GitHub Actions release on `v*` tags |
| `06-updater.md` | Update check and installer hand-off |
| `07-release-verification.md` | Release audit and 1.0.0 checklist |

Rules shared by every prompt:

- Inspect before editing; follow `CLAUDE.md`. Make focused changes; no unrelated refactors.
- Testing: focused tests for the code you touch while implementing. Run `python -m pytest -m dev` and
  `python -m ruff check .` once at the end of the prompt (not the full suite) unless told otherwise. Fix failures your
  change caused. Never delete or weaken a test to pass.
- Never read, print, bundle or commit `.env` or any secret. No personal names or e-mail addresses in publisher metadata.
- Do not commit, push, tag, publish a release or change production configuration unless explicitly asked.
- Say plainly what was automated, what was verified by hand, and what was not tested. Do not claim Windows-version,
  clean-machine or installer behaviour that was not actually exercised.
- End with a report: files changed, commands run and their results, commands the owner must run, actions needed outside
  the repository, remaining limitations.
