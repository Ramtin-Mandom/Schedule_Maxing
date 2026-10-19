# Roadmap

What is left to do, in priority order. It is built from the code at commit `6cc8855` plus its uncommitted
working tree, from the documented limitations in `docs/`, and from the README (2026-10-19). Current
status and evidence are in [CURRENT_STATE.md](CURRENT_STATE.md); file locations are in
[CODEBASE_MAP.md](CODEBASE_MAP.md).

**The project has no issue tracker or approved product backlog in the repository.** The milestone plans
in `docs/milestone-*`, `docs/next-milestone-prompts/` and `docs/productivity-redesign-plan.md` have all
been implemented, and `docs/README.md` marks them as historical. So this roadmap separates three kinds of
work:

- **Part 1, confirmed unfinished work:** defects or loose ends that can be seen in the current repository.
- **Part 2, existing plans not yet implemented:** items the repository itself names as future or not-yet-done
  work.
- **Part 3, recommended improvements requiring approval:** proposals from this analysis. They are not
  approved plans; ask the owner before starting one.

Already done; do not re-plan these: recurrence series, manual placements and explicit rescheduling,
the five scheduling modes, task types and points, the productivity tracker redesign, email verification,
MFA, password recovery, session management, database-backed rate limits, the Windows installer and
updater, and the CI, release and deploy workflows.

---

## Part 1 — Confirmed unfinished work

### M1. Make the in-progress 1.2.0 debug branch releasable

- **Priority:** High
- **Status:** In progress. Branch `codex/debug-1.2.0-update` has uncommitted changes in 11 files.
- **Objective:** Commit a green change through a pull request so that CI, and then a release, can use it.
- **Dependencies:** None.
- **Modules:** `app/ui/update_view.py`, `app/ui/update_controller.py`, `app/sync/transport.py`, `app/app.py`,
  `app/ui/ui_settings.py`, `packaging/windows/build_windows.ps1`, and their tests.
- **Deliverables:**
  - [ ] Fix Ruff F841 at `app/ui/update_view.py:130`: `release` is assigned but never used.
  - [ ] Run `pytest -m dev` and `ruff check .`, then open a pull request (CI runs only on pull requests).
  - [ ] When releasing: bump `app/version.py` and tag `v<version>`.
- **Acceptance criteria:** All five CI jobs pass on the pull request; `ruff check .` is clean.
- **Testing:** `pytest -m dev`, `ruff check .`, `tests/update/`, `tests/test_transport_security.py`,
  `tests/test_packaging_config.py`.
- **Risks:** Low.
- **Complexity:** Small.

### M2. Correct documentation that contradicts the code

- **Priority:** Medium
- **Status:** Not started.
- **Objective:** Stop agents and readers from acting on false statements.
- **Dependencies:** None.
- **Modules:** `README.md`, `docs/ci-cd.md`, `docs/desktop-day.md`, `.claude/skills/schedule-maxing/SKILL.md`.
- **Deliverables:**
  - [ ] README "Current Limitations" says "Account deletion and email verification are not provided". Email
    verification exists (`backend/verification.py`, `verification_api.py`, Alembic `0017`); only account
    deletion is missing.
  - [ ] README "Current Limitations" says the backend supports "SMTP-based password recovery". It now also
    supports the Resend, Brevo and Mailjet providers (`backend/mail.py`).
  - [ ] `docs/ci-cd.md` §1 and README "Verification" say CI runs on every push and has four jobs.
    `ci.yml` runs on `pull_request` and `workflow_dispatch` only and has five jobs (including
    `security-scan`).
  - [ ] `docs/desktop-day.md` (near line 354) describes Productivity sections "Schedule follow-through",
    "History" and "Data". `app/ui/productivity_page.py` has only General / Specific / Project.
  - [ ] The skill says the app is also on Android. No Android code exists in the repository.
- **Acceptance criteria:** Each statement matches the code it describes.
- **Testing:** None; documentation only.
- **Risks:** None.
- **Complexity:** Small.

### M3. Find and fix the intermittent Tk test failure

- **Priority:** Medium
- **Status:** Open. `docs/testing.md` says the cause is unknown.
- **Objective:** Make the real-window tier reliable, so a CI failure means a real defect.
- **Modules:** `tests/conftest.py`, `tests/tk_cleanup.py`, `app/ui/tk_lifecycle.py`.
- **Deliverables:**
  - [ ] Reproduce `TclError: invalid command name "tcl_findLibrary"` during `_tkinter.create`.
  - [ ] Fix the cause, or isolate it with a documented, narrowly scoped workaround.
- **Acceptance criteria:** Several consecutive full runs of the `ui` tier pass without the error.
- **Testing:** `pytest -m ui`, repeated.
- **Risks:** Hard to reproduce; the error may be a Tcl/Tk or Windows environment issue.
- **Complexity:** Medium.

---

## Part 2 — Existing plans not yet implemented

Items the repository names as future work or as "not yet" limitations, in priority order.

### M4. Verify backend deployment end to end

- **Priority:** High
- **Status:** Not verified. `docs/ci-cd.md` §6 says the deployment workflow "has not yet been run
  against a real Render service" and asks for the first deployment to be done by hand.
- **Dependencies:** A Render service, the `production` environment secrets, and `BACKEND_DEPLOY_ENABLED=true`.
  These are operator actions.
- **Modules:** `.github/workflows/deploy-backend.yml`, `docs/render-deployment.md`, `backend/smoke_check.py`.
- **Deliverables:**
  - [ ] Run the workflow manually once and watch the deploy, `/health` and `/ready`.
  - [ ] Run `python -m backend.smoke_check https://<host>` against the deployed service.
  - [ ] Record the result in `docs/ci-cd.md`.
- **Acceptance criteria:** A green deployment run, and a passing smoke check.
- **Testing:** The smoke check; `pytest -m postgres` against a disposable database.
- **Risks:** Production data. The pipeline takes no backup before a migration.
- **Complexity:** Small (operator time).

### M5. Code-sign releases and check signatures in the updater

- **Priority:** Medium
- **Status:** The hooks exist and nothing is signed. `build_windows.ps1` signs when it is given a certificate,
  `release.yml` reads optional secrets, and the updater's `verify_signature()` is "where it is checked once
  releases are signed".
- **Dependencies:** A code-signing certificate (an owner purchase decision).
- **Modules:** `packaging/windows/build_windows.ps1`, `ScheduleMaxing.iss`, `.github/workflows/release.yml`,
  `app/update/downloader.py`.
- **Deliverables:**
  - [ ] Configure the `SM_SIGN_*` secrets.
  - [ ] Have `verify_signature` reject unsigned or wrongly signed installers.
  - [ ] Update `docs/windows-distribution.md`.
- **Acceptance criteria:** A signed installer is accepted; a tampered or unsigned one is refused.
- **Testing:** `tests/update/test_update_service.py`, `tests/test_release_workflow.py`.
- **Risks:** Locking out installed clients if the certificate changes.
- **Complexity:** Medium.

### M6. Project archiving

- **Priority:** Low
- **Status:** Not implemented. README "Future Improvements" and "Project Status" list it;
  `docs/desktop-projects-allocation.md` says "Archive is not supported".
- **Modules:** `app/planning/models.py` (`Project`), `app/planning/repository.py`, `app/execution/db.py`
  (migration), `backend/models.py` plus an Alembic revision, `backend/resources.py`, `app/sync/mapping.py`,
  `app/ui/projects_controller.py`, `app/ui/planning_pages.py`.
- **Deliverables:**
  - [ ] An archived state on projects, stored and synced on both sides.
  - [ ] UI to archive and restore projects.
  - [ ] Filters that hide archived projects without breaking task references.
- **Acceptance criteria:** Archived projects keep their tasks and history, sync both ways, and can be restored.
- **Testing:** Migration tests on both sides, `tests/ui/test_projects_allocation.py`, and sync protocol tests.
- **Risks:** Wire-format change: older clients must tolerate the new field.
- **Complexity:** Medium.

### M7. Scheduling across DST changes and overnight windows

- **Priority:** Low
- **Status:** Refused on purpose with explicit errors (`app/planning/time.py`). `docs/sync-contract.md` §11
  lists it under "Still not implemented".
- **Modules:** `app/planning/time.py`, `app/optimizer.py`, `app/planning/fixed_block_rules.py`, the task form, and
  the Day and Week timelines.
- **Deliverables:**
  - [ ] A design decision record first, because this changes ADR-034.
  - [ ] Engine support for elapsed-minute windows.
  - [ ] UI positioning on transition days.
- **Acceptance criteria:** Correct placement on spring-forward and fall-back dates; no silent truncation.
- **Testing:** `tests/planning/test_time.py`, `test_deadline_timezones.py`, `tests/test_day_engine.py`.
- **Risks:** High. This touches hard constraints and stored intervals.
- **Complexity:** Large.

### M8. Smaller documented gaps

- **Priority:** Low
- **Status:** Each item is recorded as a limitation in the doc cited after it.
- **Deliverables:**
  - [ ] Preference layers in the planning CSV (`docs/sync-contract.md` §11).
  - [ ] Store the reasons tasks were left unplaced, so they survive a restart (`docs/desktop-day.md` "Limitations").
  - [ ] Decide whether regeneration should cancel the `scheduled` execution of a placement it replaces
    (`docs/execution-rescheduling.md` §10).
  - [ ] `op_id` replay for REST placement operations (`docs/execution-rescheduling.md` §10).
  - [ ] A desktop view of other workspaces' records (`docs/desktop-web-boundaries.md` §8).
  - [ ] A staging environment and backups before migrations for the backend (`docs/ci-cd.md` §6).
- **Testing:** The tests named for each area in [CODEBASE_MAP.md](CODEBASE_MAP.md#d-if-you-need-to-change-x-look-here).
- **Complexity:** Small to Medium each.

### M9. Optional engine ideas listed in the README

- **Priority:** Low
- **Status:** The README lists these as "Possible next steps"; they are not commitments.
- **Items:**
  - a simulated-annealing optimizer (its settings exist, unused, in `config/settings.py`);
  - schedule comparison metrics;
  - more detailed reasons for unscheduled tasks;
  - warnings for ignored missing dependencies (legacy pipeline);
  - drag-and-drop editing;
  - opt-in use of predicted durations;
  - showing ML predictions in the UI once the evidence gate passes.
- **Complexity:** Varies.

---

## Part 3 — Recommended improvements requiring approval

These come from this analysis. None of them has been approved.

### R1. Account deletion and data-retention compaction

- **Priority:** High (privacy and data integrity)
- **Status:** Not implemented and not planned. There is no account-deletion route, and `backend/maintenance.py`
  deliberately never removes accounts, records, tombstones or the change log.
- **Proposal:**
  - an authenticated, re-authenticated `DELETE /me` that removes the user's records, sessions and outbox
    rows;
  - a bounded policy for compacting tombstones and the change log that respects sync cursors.
- **Modules:** `backend/api.py`, `account_security.py`, `maintenance.py`, `models.py`, `sync.py`;
  `app/sync/service.py`; `app/ui/account_page.py`.
- **Risks:** Compaction can break offline clients' pulls. The protocol needs a "resync required" answer.
- **Complexity:** Large.

### R2. Plan the move off Python 3.10

- **Priority:** Medium
- **Status:** Python 3.10 is pinned everywhere (ADR-002). Its upstream security support is scheduled to end
  in October 2026 (PEP 619).
- **Proposal:** Pick a newer version and change all workflows, `build_windows.ps1` and its test, and the
  `.venv` in one change. Re-check PyInstaller, CustomTkinter and the scikit-learn wheels.
- **Risks:** Changes in bundling behavior; checks needed on a clean Windows machine.
- **Complexity:** Medium.

### R3. Wire up or remove the UI modules nothing can reach

- **Priority:** Medium
- **Status:**
  - `app/ui/feedback_dialog.py`: focus, energy and interruption feedback. The README lists this as a feature,
    and `ExecutionController.record_feedback` supports it.
  - `app/ui/cohort_view.py`: schedule follow-through. `ProductivityController.schedule_cohort_for_last` exists.
  - `app/ui/duration_suggestion.py`: the median duration suggestion. The README lists this as a feature.
  - The `RewardConfigPage` class in `app/app.py`.

  None of these is imported or instantiated by the running app.
- **Proposal:** The owner decides whether to bring the feedback dialog, the follow-through panel and the
  duration suggestion back into the 1.1+ UI, or to delete them and correct the README. Remove
  `RewardConfigPage`, which `README.md` says is no longer exposed.
- **Testing:** `tests/ui/test_task_editor_widgets.py` covers `duration_suggestion`. Add page tests if the
  modules are wired in.
- **Complexity:** Small for removal; Medium for each module wired in.

### R4. Split the largest modules

- **Priority:** Low
- **Status:**
  - `app/planning/application.py` (about 2,500 lines), `app/optimizer.py` (about 2,000; two engines in one
    module), `app/execution/db.py` (about 1,600; it owns planning and sync tables although it sits in
    `execution/`), `backend/models.py` (about 1,600).
  - `app/planning/application.py` imports `app.execution.db`, which couples the two packages.
- **Proposal:**
  - Move the legacy Greedy v1 into its own module, behind a re-export.
  - Move the storage infrastructure into a neutral package, with shims at the old import paths.
  - Make each move behavior-preserving, guarded by the existing baselines.
- **Risks:** Wide import changes, and moves that look like rewrites in review. Do this only with approval and
  in its own pull request.
- **Complexity:** Medium.

### R5. Tighten the desktop dependency bounds

- **Priority:** Low
- **Status:** `requirements-desktop.txt` has lower bounds only. `docs/desktop-web-boundaries.md` §8 records
  that a fresh install resolved CustomTkinter 6.0.0 while the development environment has 5.2.2.
  `requirements.lock` exists for the security scan, but the build does not use it.
- **Proposal:** Build the installer from the lock file, or add upper bounds, so released builds can be
  reproduced.
- **Complexity:** Small.

### Not on the roadmap without explicit owner direction

- An Android client (the README author note mentions it as a goal; no code exists).
- A browser web UI (`docs/web-api.md` is its contract; no frontend code is in the repository).
- Billing or plans (`docs/web-api.md`: "Billing is not implemented").
