# Roadmap

Open work and completed milestones, updated during 1.3.1 preparation on 2026-11-24. M4 tracks the
installer and hosted backend cutover. R2 records the Python 3.12 upgrade.
See [repository diagnosis](history/diagnosis.md), [current state](CURRENT_STATE.md), and
[codebase map](FEATURE_MAP.md).

Past project plans and reports live in `docs/history/`. This roadmap records confirmed work, existing plans
and proposals that still need a product decision.

- **Part 1, confirmed unfinished work:** defects or loose ends that can be seen in the current repository.
- **Part 2, existing plans not yet implemented:** items the repository itself names as future or not-yet-done
  work.
- **Part 3, recommended improvements requiring approval:** proposals from this analysis. They are not
  approved plans; ask the owner before starting one.

Already done; do not re-plan these: recurrence series, manual placements and explicit rescheduling,
the five scheduling modes, task types and points, the productivity tracker redesign, desktop budgeting
(balance, income/expenses, stats and forecast, future items), email verification,
MFA, password recovery, session management, database-backed rate limits, the Windows installer and
updater, and the CI, release and deploy workflows.

---

## Part 1 — Confirmed unfinished work

### M1. Stabilize the 1.2.0 debug changes for the 1.3.0 release

- **Priority:** High
- **Status:** Complete (verified 2026-10-23): pull request #23 merged with all five CI jobs green, and
  1.3.0 was released from `main` (`dfd53d6`, tag `v1.3.0`). Earlier: pull request #21 merged the branch into `main` (including the build script's
  Python 3.10 check); pull request #22 merged the remaining updater edits and the Ruff fix. `ruff check .`
  is clean on `664de0e` (verified 2026-10-20); the pull request's CI results were not checked here.
- **Objective:** Commit a green change through a pull request so that CI, and then a release, can use it.
- **Dependencies:** None.
- **Modules:** `app/ui/update_view.py`, `app/ui/update_controller.py`, `app/sync/transport.py`, `app/app.py`,
  `app/ui/ui_settings.py`, `packaging/windows/build_windows.ps1`, and their tests.
- **Deliverables:**
  - [x] Fix Ruff F841 at `app/ui/update_view.py:130`: `release` is assigned but never used (`800996f`).
  - [x] Run `pytest -m dev` and `ruff check .`, then open a pull request (CI runs only on pull requests).
    Pull request #23 passed `test`, `security-scan`, `desktop-only`, `direct-desktop` and `postgres`.
  - [x] When releasing: bump `app/version.py` and tag `v<version>` (1.3.0, `v1.3.0`).
- **Acceptance criteria:** All five CI jobs pass on the pull request; `ruff check .` is clean.
- **Testing:** `pytest -m dev`, `ruff check .`, `tests/update/`, `tests/test_transport_security.py`,
  `tests/test_packaging_config.py`.
- **Risks:** Low.
- **Complexity:** Small.

### M2. Correct documentation that contradicts the code

- **Priority:** Medium
- **Status:** Complete in the 2026-11-23 documentation audit. The README describes verified accounts and the
  configured mail backends. The CI guide lists the five jobs and trigger events. The Day guide reflects the
  current Productivity views and outcome controls. The project navigation skill identifies Windows as the
  delivered desktop platform.

### M3. Find and fix the intermittent Tk test failure

- **Priority:** Medium
- **Status:** Open. The latest PR CI runs passed. The cause of the historical intermittent failure remains
  unknown. [Testing guide](development/testing.md#known-flaky-test)
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

### M4. Verify the first post-1.3.0 release and backend cutover

- **Priority:** High
- **Status:** The 1.3.1 desktop version is prepared for SQLite v19. A read-only check confirmed the local
  desktop database is already on v19. The backend reported ready during the audit. Its deployed build and
  schema revision remain unverified. [Published Windows releases](https://github.com/Ramtin-Mandom/Schedule_Maxing/releases)
- **Dependencies:** Render service access, a Python 3.12 build runtime, a tested database backup and restore
  plan, and an operator who can coordinate the cutover. The tag-triggered workflow also requires
  `BACKEND_DEPLOY_ENABLED=true`.
- **Modules:** `.github/workflows/deploy-backend.yml`, `docs/deployment/render-deployment.md`, `backend/smoke_check.py`.
- **Deliverables:**
  - [ ] Identify the active server commit and Alembic revision. Verify a backup and restore on disposable data.
  - [ ] Rehearse the `0021` deployment with the old service out of traffic. Verify health, readiness and the
    authenticated smoke check on a disposable or staging service.
  - [x] Bump the desktop version to 1.3.1 for SQLite v19 compatibility.
  - [ ] Build and verify a fresh install and an upgrade from 1.3.0 using a copy of a v18 database. Exercise
    old-client sync against the upgraded API before marking the backend cutover complete.
  - [ ] Record the production cutover and installer verification. [CI/CD guide](deployment/ci-cd.md#migrations)
- **Acceptance criteria:** A ready server at the intended revision, a tested rollback plan, and a published
  installer that upgrades 1.3.0 without losing supported data.

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
  - [ ] Update `docs/deployment/windows-distribution.md`.
- **Acceptance criteria:** A signed installer is accepted; a tampered or unsigned one is refused.
- **Testing:** `tests/update/test_update_service.py`, `tests/test_release_workflow.py`.
- **Risks:** Locking out installed clients if the certificate changes.
- **Complexity:** Medium.

### M6. Project archiving

- **Priority:** Low
- **Status:** Not implemented. README "Future Improvements" and "Project Status" list it;
  `docs/features/desktop-projects-allocation.md` says "Archive is not supported".
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
- **Status:** Refused on purpose with explicit errors (`app/planning/time.py`). `docs/architecture/sync-contract.md` §11
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
  - [ ] Preference layers in the planning CSV (`docs/architecture/sync-contract.md` §11).
  - [ ] Store the reasons tasks were left unplaced, so they survive a restart (`docs/features/desktop-day.md` "Limitations").
  - [x] Cancel a never-started execution when regeneration removes its placement. This is implemented as
    a `superseded` cancellation. [Execution and rescheduling](features/execution-rescheduling.md#5-regeneration-audit)
  - [ ] `op_id` replay for REST placement operations (`docs/features/execution-rescheduling.md` §10).
  - [ ] A desktop view of other workspaces' records (`docs/architecture/desktop-web-boundaries.md` §8).
  - [ ] A staging environment and backups before migrations for the backend (`docs/deployment/ci-cd.md` §6).
- **Testing:** The tests named for each area in [FEATURE_MAP.md](FEATURE_MAP.md#d-if-you-need-to-change-x-look-here).
- **Complexity:** Small to Medium each.

### M9. Scheduling improvements

- **Priority:** Medium
- **Status:** The audit reproduced a range assignment with adequate aggregate minutes and no contiguous slot,
  even though a different date in the range was free.
- **Proposal:** Decide whether to detect the blocked date during allocation or retry unplaced tasks against
  eligible alternative dates. Keep the day engine's constraints authoritative. [Reproduction](history/diagnosis.md#4-range-allocation-can-miss-work-the-day-engine-could-place-elsewhere)

### M10. Budgeting beyond the desktop

- **Priority:** Low
- **Status:** Budgeting is implemented end to end on the desktop (General, Stats, Future, the Day page money
  widgets), with local, direct and synchronized storage. Only the local web profile lacks it.
- **Deliverables:**
  - [x] Desktop Budgeting page and Day page integration.
  - [ ] Budget routes in the local web profile, if wanted.
  - [ ] Optional: "this and following" series edits, moving an occurrence from the forms.
- **Testing:** `tests/budget/*`, `tests/ui/test_budget_*.py`.

### M11. Drop the legacy `priority` / `dependency_ids` wire placeholders

- **Priority:** Low
- **Status:** Waiting. Tasks and executions have no priority or dependencies since ADR-048, but 1.3.0 clients
  fail on a pulled record without the fields, so the server and the client still send `priority` (5) and
  `dependency_ids` ([]) and ignore them on input.
- **Dependencies:** Server revision `0021` deployed together with the client that removed them; no 1.3.0
  client left (the updater moves installed clients forward).
- **Modules:** `backend/resources.py` (`LEGACY_TASK_WIRE_FIELDS`), `backend/executions.py`
  (`LEGACY_EXECUTION_WIRE_FIELDS`), `app/sync/mapping.py` (`_with_legacy_task_wire`), `docs/architecture/sync-protocol.md`.
- **Deliverables:**
  - [ ] Remove the placeholder fields and their validators from the server schemas and the client mapping.
- **Testing:** `tests/sync/test_protocol.py`, `tests/backend/test_resources.py`, `tests/backend/test_sync_push.py`.
- **Risks:** An old client still syncing breaks on pull.
- **Complexity:** Small.
- **Complexity:** Small to Medium.

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

### R2. Move off Python 3.10

- **Status:** Implemented for 1.3.1. CI, the Windows build, Ruff and developer setup use Python 3.12.
  The backend and direct-mode requirements and the audit lock use patched dnspython 2.9.2. The audit
  runs without CVE exceptions. [ADR-052](DECISIONS.md#adr-052--python-312-is-the-tested-and-packaged-runtime)
- **Remaining verification:** Complete CI and the Windows installer smoke test. Confirm the active
  Render service uses Python 3.12 under M4. [Release CI](https://github.com/Ramtin-Mandom/Schedule_Maxing/pull/26/checks)

### R3. Remove unused desktop presentation paths

- **Status:** Complete locally. PR #25 removed the unused widgets. The follow-up removed the retired Execute,
  History and Data presentation code and their raw history exporter, the unexposed median estimator, and
  legacy range and freshness helpers. The development suite, scheduling baseline and targeted service
  checks passed. [Current verification](CURRENT_STATE.md#d-testing-and-quality)
- **Dependency for future work:** If the desktop gains timed work controls, build them on the retained
  execution lifecycle. The tracker already consumes recorded sessions. [Execution workflow](Workflow.md#outcomes-placement-moves-and-productivity)

### R4. Split the largest modules

- **Priority:** Low
- **Status:**
  - `app/planning/application.py` (about 2,500 lines), `app/optimizer.py` (about 1,300; the canonical engine only since ADR-047), `app/execution/db.py` (about 1,600; it owns planning and sync tables although it sits in
    `execution/`), `backend/models.py` (about 1,600).
  - `app/planning/application.py` imports `app.execution.db`, which couples the two packages.
- **Proposal:**
  - Move the storage infrastructure into a neutral package, with shims at the old import paths.
  - Make each move behavior-preserving, guarded by the existing baselines.
- **Risks:** Wide import changes, and moves that look like rewrites in review. Do this only with approval and
  in its own pull request.
- **Complexity:** Medium.

### R5. Tighten the desktop dependency bounds

- **Priority:** Low
- **Status:** CustomTkinter is now capped below 6 (`b5668ff`). The other entries in
  `requirements/desktop.txt` have lower bounds only. `docs/architecture/desktop-web-boundaries.md` §8 records that a fresh
  install had resolved CustomTkinter 6.0.0 while the development environment has 5.2.2.
  `requirements/requirements.lock` exists for the security scan, but the build does not use it.
- **Proposal:** Use constraints or a release lock that covers the dependencies actually installed by CI
  and the Windows build. Audit those resolved versions.
- **Complexity:** Small.

### Not on the roadmap without explicit owner direction

- An Android client (the README author note mentions it as a goal; no code exists).
- A browser web UI (`docs/backend/web-api.md` is its contract; no frontend code is in the repository).
- Billing or plans (`docs/backend/web-api.md`: "Billing is not implemented").
