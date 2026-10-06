# Prompt 5 — GitHub Actions release pipeline

Implement only this prompt, then stop with a completion report. Prerequisites: Prompts 3 and 4. Read `CLAUDE.md`,
`docs/milestone-9-prompts/README.md` and `00-audit-and-architecture.md` (sections 1.2, 5), inspect `git status`,
`.github/workflows/ci.yml` and `packaging/windows/` before editing. Do not commit, push, tag or publish anything, and do
not read `.env`.

Objective: pushing a tag `vX.Y.Z` runs the tests, builds the Windows application and installer on a Windows runner,
smoke-tests the packaged app, and only then publishes a GitHub Release with `ScheduleMaxing-Setup-X.Y.Z.exe` and
`SHA256SUMS.txt`. Any failure publishes nothing.

Audit findings addressed: CI is Linux-only and nothing builds on Windows (1.2); the version must come from one source
(decision 4); the release channel must contain only approved stable builds (decision 3).

Inspect first: `.github/workflows/ci.yml` (keep it as it is), `packaging/windows/build_windows.ps1`,
`ScheduleMaxing.iss`, `smoke_test.py`, `app/version.py`, `docs/testing.md`.

Required work:

1. `.github/workflows/release.yml`:
   - triggers: `push` of tags matching `v[0-9]+.[0-9]+.[0-9]+` and `workflow_dispatch` (a manual run builds and uploads
     workflow artifacts only; it never creates a release);
   - top-level `permissions: contents: read`; only the publishing job gets `contents: write`;
   - job `verify` (Windows runner, Python 3.10): fail unless the tag equals `v` + the version in `app/version.py`, and
     fail if a release with that tag already exists;
   - job `test` (Linux, mirrors the existing CI test job): the full suite, compileall and ruff — a release is a
     milestone, so the complete suite belongs here;
   - job `build` (Windows runner, needs `verify` and `test`): install Inno Setup through a pinned, checksum-verified
     method (prefer the copy preinstalled on the runner image if present, otherwise `choco install innosetup` with a
     pinned version); run `build_windows.ps1`; run the bundle checks and the packaged smoke test; silently install the
     built installer on the runner, run the installed executable's self-test, silently uninstall and assert the test
     data directory survives; upload the installer and `SHA256SUMS.txt` as workflow artifacts;
   - optional signing step, enabled only when the signing secrets are defined; with none defined the workflow logs
     "unsigned build" and continues. Secrets are referenced by name only;
   - job `publish` (needs `build`, tag pushes only): create the GitHub Release for the tag as a non-draft,
     non-prerelease release with generated notes and exactly the two assets. Use the `gh` CLI with the job token rather
     than an unpinned third-party action; pin every action that is used to a major version at least.
   - `concurrency` so two runs for one tag cannot race.
2. Keep `ci.yml` unchanged apart from, at most, a small Windows job that runs the packaging configuration tests — only
   if it is cheap. Do not weaken or remove any existing job or test.
3. `docs/windows-distribution.md`, section "Releasing": the exact sequence (bump `app/version.py`, merge, tag, push the
   tag), what the workflow checks, where artifacts appear, how a manual dry run works, and "Withdrawing a release":
   mark the release as pre-release or delete it (clients read only the latest stable release, so they stop being
   offered it), then publish a higher version with the fix. State plainly that clients never downgrade automatically,
   so users who already installed the bad version are fixed only by the higher version.
4. Record the secret and variable names the signing step expects, and what the owner must configure in the repository
   settings (Actions permissions for creating releases, tag protection for `v*` if wanted).

Constraints: no secrets or tokens in the workflow file; no publishing on branch pushes or pull requests; the workflow
must not modify the repository contents.

Tests to add (`dev` tier): a test that parses `release.yml` with PyYAML and asserts the tag filter, that `publish`
depends on `build` which depends on `test` and `verify`, that only `publish` has write permission, that no step
references `.env`, and that the tag/version check exists. Extend `tests/test_packaging_config.py` or add
`tests/test_release_workflow.py`.

Verification: the workflow cannot be executed from this machine. Validate the YAML by parsing it, run the new test,
then once `python -m pytest -m dev` and `python -m ruff check .`. Run the tag/version check script locally with a
matching and a mismatching tag. Say explicitly that the workflow itself is untested until the owner triggers a manual
run.

Completion report: files changed; how to do a dry run (`workflow_dispatch`); the exact release commands; repository
settings and secrets the owner must configure; what is untested.
