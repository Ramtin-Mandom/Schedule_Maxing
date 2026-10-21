# Architecture decision records

Established design choices, so that agents do not re-litigate or silently reverse them. Each record
cites where the choice is visible and where its reasoning is written down.

**Status meanings.** *Accepted*: the choice is stated deliberately in project docs, module docstrings,
agent instructions or commit messages. *Inferred*: reconstructed from code, with no recorded rationale.
*Superseded*: replaced by a later record. *Proposed*: not yet adopted.

Reasoning is quoted or summarised only from the cited evidence. "Rationale not recorded" means the code
shows the choice but nothing explains it; do not invent one.

To change a decision, add a new record that supersedes the old one, and update the old record's status.
Records were reconstructed on 2026-10-19 from commit `6cc8855`; the IDs do not reflect when each
decision was made.

## Index

| ID | Title | Status |
|---|---|---|
| ADR-001 | Python desktop app built on CustomTkinter | Inferred |
| ADR-002 | Python 3.10 is the single supported runtime | Accepted |
| ADR-003 | Hard constraints decide validity; reward only ranks valid placements | Accepted |
| ADR-004 | Greedy Optimizer v1 is kept as a frozen baseline | Accepted |
| ADR-005 | Canonical planning models run in parallel with the legacy models | Accepted |
| ADR-006 | Scheduling modes are objectives over one greedy engine | Accepted |
| ADR-007 | Deterministic, bounded heuristics instead of a global optimizer | Accepted |
| ADR-008 | All local data lives in one SQLite database, with no in-memory fallback | Accepted |
| ADR-009 | Ordered in-code SQLite migrations, a backup before each, newer schemas refused | Accepted |
| ADR-010 | One process per local database file | Accepted |
| ADR-011 | One shared domain layer; the HTTP adapters are optional | Accepted |
| ADR-012 | The desktop runs with no server packages; client HTTP uses the standard library | Accepted |
| ADR-013 | Records are sync-ready: UUIDs, owners, versions and tombstones | Accepted |
| ADR-014 | Offline-first sync: trigger capture, outbox, shadows, explicit conflicts | Accepted |
| ADR-015 | Guest data is associated with an account only by an explicit step | Accepted |
| ADR-016 | Every server write goes through one Mutator | Accepted |
| ADR-017 | Server backend on FastAPI with PostgreSQL, SQLAlchemy 2 and Alembic | Accepted |
| ADR-018 | Normalized server storage with immutable revisions, via expand/backfill/contract | Accepted |
| ADR-019 | Authentication: Argon2id, short JWT access tokens, rotating refresh credentials | Accepted |
| ADR-020 | Access tokens stay in memory; the saved refresh credential goes in the OS vault | Accepted |
| ADR-021 | Rate-limit counters live in the database | Accepted |
| ADR-022 | Email goes through a durable outbox; HTTPS providers are supported | Accepted |
| ADR-023 | Password policy: at least 8 characters and common passwords refused | Accepted |
| ADR-024 | Direct PostgreSQL storage is a development-only mode | Accepted |
| ADR-025 | All desktop storage work runs off the Tk thread on a bounded worker pool | Accepted |
| ADR-026 | Cyclic garbage collection runs on the Tk thread while windows are alive | Accepted |
| ADR-027 | Controllers return ControllerResult instead of raising into Tk | Accepted |
| ADR-028 | `app/runtime.py` is the only source-versus-packaged switch | Accepted |
| ADR-029 | The version is written once, and the release tag must match it | Accepted |
| ADR-030 | Windows delivery: PyInstaller folder build, Inno Setup, signing optional | Accepted |
| ADR-031 | Updater trusts GitHub over HTTPS plus the published SHA-256 | Accepted |
| ADR-032 | CI runs once per pull request; deploys and releases reuse that result | Accepted |
| ADR-033 | Automatic test tiers; the fast `dev` suite while working; no parallel tests | Accepted |
| ADR-034 | One planning time zone; DST-transition and overnight windows are refused | Accepted |
| ADR-035 | Points replace priority, and task kinds are Flexible, Fixed and To Do | Accepted |
| ADR-036 | Analytics are recomputed from records; executions snapshot task fields | Accepted |
| ADR-037 | The ML duration predictor is evidence-gated and kept out of the UI | Accepted |
| ADR-038 | The local web profile listens on loopback only, behind a session guard | Accepted |
| ADR-039 | Budget records: a separate domain, integer minor units, expanded recurrence, optional sync feature | Accepted |
| ADR-040 | Budget forecast: a balance snapshot plus a 15-day one-time spending average; future items are not transactions | Accepted |
| ADR-041 | The background sync cadence follows the desktop window's activity | Accepted |
| ADR-042 | Local SQLite keeps the rollback journal: WAL evaluated and not adopted; no new indexes | Accepted |
| ADR-043 | Long lists and static text use one-window light widgets | Accepted |
| ADR-044 | The desktop runs the day engine in a separate process | Accepted |
| ADR-045 | Light caching: an opt-in mode that keeps only Day and the open section built | Accepted |

---

### ADR-001 — Python desktop app built on CustomTkinter

- **Status:** Inferred
- **Context:** The product needed a desktop UI.
- **Decision:** CustomTkinter on Tk, plus `ttk.Treeview`, all in pure Python.
- **Rationale:** No technical comparison is recorded. The README's author note says the project's goal was to learn the workflow of building a desktop application (and, as an aspiration, Android).
- **Alternatives:** Not documented.
- **Consequences:** It needs no web toolchain ("No npm build or browser is needed", README). Tk is single-threaded, which drives ADR-025 and ADR-026. Real-window tests are slow (about 420 s, `docs/testing.md`).
- **Relevant files:** `app/app.py`, `app/ui/`, `requirements-desktop.txt`.
- **Evidence:** README "Milestone 4 desktop presentation"; `docs/testing.md`.

### ADR-002 — Python 3.10 is the single supported runtime

- **Status:** Accepted
- **Context:** Testing on one Python version and building the installer on another would ship a program that the tests never checked.
- **Decision:** CI, the release build and local builds all use Python 3.10. `build_windows.ps1` refuses any other version, and a test keeps the script's required version equal to the workflows' `python-version`.
- **Rationale:** The comment in `build_windows.ps1` says the installer must hold the same program the tests checked.
- **Alternatives:** Not documented.
- **Consequences:** Moving to a newer Python means changing every workflow and the build script together.
- **Relevant files:** `.github/workflows/*.yml`, `packaging/windows/build_windows.ps1`, `tests/test_packaging_config.py`.
- **Evidence:** `test_build_script_requires_the_python_that_ci_tests_with`; the skill note in `.claude/skills/schedule-maxing-debugging/SKILL.md`. The build-script change is committed in `0ec34de`.

### ADR-003 — Hard constraints decide validity; reward only ranks valid placements

- **Status:** Accepted
- **Context:** Constraints (day window, fixed blocks, overlaps, durations, dependencies, deadlines) and preferences could be mixed together in one score.
- **Decision:** Constraint modules answer "is this placement allowed?". `reward.py` answers "how good is it?". A hard constraint is never turned into a penalty.
- **Rationale:** This is the README's stated core idea, and both agent instruction files restate it as a rule.
- **Alternatives:** Soft-constraint penalties (rejected by the rule itself).
- **Consequences:** Infeasible work is reported as unscheduled with a reason, never placed illegally. Preferred time (Early/Mid/Late) is a strong preference, not a constraint.
- **Relevant files:** `app/constraints.py`, `app/planning/fixed_block_rules.py`, `app/pert.py`, `app/reward.py`, `app/optimizer.py`.
- **Evidence:** README "Project Overview"; `AGENTS.md` section 3; `CLAUDE.md` "Architecture".

### ADR-004 — Greedy Optimizer v1 is kept as a frozen baseline

- **Status:** Accepted
- **Context:** The original Milestone 0 engine was superseded by the canonical day engine.
- **Decision:** Keep `optimize_day_schedule` unchanged, apart from narrowly scoped documented fixes, as a protected baseline. New behavior goes into the canonical engine.
- **Rationale:** The module docstring calls it "the frozen baseline scheduling algorithm"; the agent instructions require preserving optimizer baselines.
- **Alternatives:** Not documented.
- **Consequences:** Two engines in one module. The legacy CSV loader keeps known quirks (the hyphen split) that tests pin as current behavior.
- **Relevant files:** `app/optimizer.py`, `app/models.py`, `app/data_processor.py`, `benchmarks/optimizer_baseline.py`.
- **Evidence:** `app/optimizer.py` docstring; README "Current Limitations"; `CLAUDE.md`.

### ADR-005 — Canonical planning models run in parallel with the legacy models

- **Status:** Accepted
- **Context:** The legacy models use abstract day indexes and name-based dependencies.
- **Decision:** A new canonical model in `app/planning/models.py` (calendar dates, UUID identity, aware datetimes) sits beside `app/models.py`, connected by explicit adapters in `compat.py`.
- **Rationale:** The docstring says it is "a *new*, parallel domain model, not a replacement" and that the legacy layer stays as the compatibility layer during migration.
- **Alternatives:** Not documented.
- **Consequences:** The desktop, CLI and server use the canonical models; legacy models remain for Greedy v1, legacy CSV and exports.
- **Relevant files:** `app/planning/models.py`, `app/models.py`, `app/planning/compat.py`.
- **Evidence:** Module docstrings of `app/planning/models.py` and `app/planning/compat.py`.

### ADR-006 — Scheduling modes are objectives over one greedy engine

- **Status:** Accepted
- **Context:** Different users want different schedule shapes.
- **Decision:** Five modes (Normal, ADHD, Early Finish, Night Owl, Catch-Up) share one engine and one set of hard constraints, and differ only in their objective. The stored values `precise_greedy`/`adhd_friendly` keep their meaning.
- **Rationale:** The docs say "A mode is an objective over one shared search, not a separate optimizer"; saved choices and override precedence stay unchanged.
- **Alternatives:** Separate optimizers per mode (rejected by the doc's framing).
- **Consequences:** A new mode needs an enum value, an objective, and new check-constraint migrations on both the SQLite and PostgreSQL sides.
- **Relevant files:** `app/planning/preferences.py`, `app/optimizer.py`, `app/mode_objectives.py`, `app/planning/catch_up.py`.
- **Evidence:** `docs/scheduling-modes.md`; the `OptimizerMode` docstring.

### ADR-007 — Deterministic, bounded heuristics instead of a global optimizer

- **Status:** Accepted
- **Context:** Schedule quality has to be weighed against speed and predictability.
- **Decision:** A greedy event-candidate search, with bounded repacking for Early Finish and Night Owl (every order up to 6 tasks, otherwise two orders). The baseline result always competes, so the objective never gets worse.
- **Rationale:** Documented as "a bounded heuristic with no global-optimum guarantee", checked against an exhaustive search on small fixtures.
- **Alternatives:** Simulated annealing is listed as a possible future improvement in the README; its settings exist in `config/settings.py` but no engine uses them.
- **Consequences:** Schedules are good, not optimal; results are reproducible.
- **Relevant files:** `app/optimizer.py`, `app/mode_objectives.py`.
- **Evidence:** `docs/scheduling-modes.md`; `docs/milestone-6-completion.md` "Remaining limitations"; README "Future Improvements".

### ADR-008 — All local data lives in one SQLite database, with no in-memory fallback

- **Status:** Accepted
- **Context:** Before Milestone 2, data lived in CSV files and memory.
- **Decision:** Every task, fixed block, schedule and execution is stored in one per-user SQLite file. If it cannot be opened, the app shows an error instead of an unsaved scheduler.
- **Rationale:** The docs say an in-memory fallback's edits "would silently never be saved"; the README says "Nothing lives only in memory".
- **Alternatives:** The CSV/in-memory storage that came before.
- **Consequences:** One connection behind a lock, so concurrency is serialized. The migration code lives in `app/execution/db.py` even though it covers planning tables too.
- **Relevant files:** `app/execution/db.py`, `app/ui/app_services.py`, `config/settings.py`.
- **Evidence:** The `app_services.py` docstring; README "Since Milestone 2".

### ADR-009 — Ordered in-code SQLite migrations, a backup before each, newer schemas refused

- **Status:** Accepted
- **Context:** User databases must survive app updates.
- **Decision:** Version-numbered migrations in `MIGRATIONS` run in ascending order inside transactions. The file is copied first. A database whose schema is newer than the code is refused.
- **Rationale:** The `backup.py` docstring says the copy exists "so an application update can never be the reason data is lost".
- **Alternatives:** Not documented.
- **Consequences:** Migrations are append-only, and each version has its own migration test.
- **Relevant files:** `app/execution/db.py`, `app/execution/backup.py`, `tests/execution/test_migration_v*.py`.
- **Evidence:** Module docstrings of `db.py` and `backup.py`.

### ADR-010 — One process per local database file

- **Status:** Accepted
- **Context:** The desktop and the local web profile could open the same file at the same time.
- **Decision:** A lock file per database; a second process is refused with a clear message. A separate Windows mutex only tells the installer that the app is running.
- **Rationale:** Recorded in `docs/desktop-web-boundaries.md`, which notes that nothing previously stopped two processes from sharing one database's sync session.
- **Alternatives:** Not documented.
- **Consequences:** Running the desktop and the local web profile on one database at once is unsupported.
- **Relevant files:** `app/execution/instance_lock.py`, `app/desktop.py`.
- **Evidence:** `docs/desktop-web-boundaries.md` sections 1 and 8; `docs/windows-distribution.md` "Known limitations".

### ADR-011 — One shared domain layer; the HTTP adapters are optional

- **Status:** Accepted
- **Context:** The desktop, the hosted API and the local web profile all need scheduling and persistence rules.
- **Decision:** "One engine, one set of domain services, two optional HTTP adapters." The desktop calls services directly and never calls its own HTTP routes. The server implements the planning repository interface and reuses `workflow.py`.
- **Rationale:** Set out in `docs/desktop-web-boundaries.md`. Its audit found operations that existed only inside HTTP route bodies and moved them into the shared layer.
- **Alternatives:** A desktop that talks to a local server (rejected there).
- **Consequences:** `app.planning` must stay free of Tk, FastAPI and SQLAlchemy imports.
- **Relevant files:** `app/planning/workflow.py`, `app/planning/application.py`, `backend/planning_repository.py`, `backend/planning_api.py`, `app/web/`.
- **Evidence:** `docs/desktop-web-boundaries.md`; `tests/test_desktop_isolation.py`.

### ADR-012 — The desktop runs with no server packages; client HTTP uses the standard library

- **Status:** Accepted
- **Context:** The packaged client must stay small and must never carry server code or database drivers.
- **Decision:** `requirements-desktop.txt` excludes FastAPI, SQLAlchemy, psycopg, Alembic, PyJWT, argon2 and HTTPX. Sync and the updater use `urllib`. Optional packages are imported lazily.
- **Rationale:** `transport.py` says it is designed so that "the desktop app gains no network dependency". `check_bundle.py` and the `desktop-only` CI job enforce the rule.
- **Alternatives:** Not documented.
- **Consequences:** A top-level server import anywhere in `app/` (outside the direct and web modules) breaks CI and the build.
- **Relevant files:** `app/sync/transport.py`, `app/update/transport.py`, `packaging/windows/check_bundle.py`, `ScheduleMaxing.spec` (excludes).
- **Evidence:** `docs/ci-cd.md` (the `desktop-only` job); the `.claude/skills/schedule-maxing` skill.

### ADR-013 — Records are sync-ready: UUIDs, owners, versions and tombstones

- **Status:** Accepted
- **Context:** Local records had to become synchronizable without losing offline use.
- **Decision:** Every synchronizable record has a UUID, an owner (`user_id`, where NULL means the guest workspace), a local `version`, and a deletion marker. A stale edit is refused rather than overwriting newer data.
- **Rationale:** Recorded in `docs/sync-contract.md` and in the README's Milestone 3 status.
- **Alternatives:** Not documented.
- **Consequences:** Deletes are soft. The tombstones and change log are never compacted (see [ROADMAP.md](ROADMAP.md)).
- **Relevant files:** `app/planning/models.py`, `app/planning/scope.py`, `app/execution/db.py`.
- **Evidence:** `docs/sync-contract.md`; README "Project Status".

### ADR-014 — Offline-first sync: trigger capture, outbox, shadows, explicit conflicts

- **Status:** Accepted
- **Context:** Several devices edit the same records, sometimes offline.
- **Decision:**
  - SQLite triggers mark changes in the same transaction as the write.
  - Operations go into an outbox with stable `op_id`s.
  - Versions sent to the server are the last acknowledged server version (the shadow).
  - Groups of operations are atomic, and the server records outcomes so that a retry is idempotent.
  - Conflicts are stored and resolved explicitly by the user (`accept_remote` / `keep_local`).
- **Rationale:** `docs/sync-protocol.md` explains each part: a rolled-back write leaves no mark, retries never apply twice, and pulled data never echoes back.
- **Alternatives:** Not documented.
- **Consequences:** Any change to records or the wire format must be made in the client and server together. A deleted record is never revived.
- **Relevant files:** `app/sync/`, `backend/sync.py`, `backend/mutations.py`, `backend/snapshots.py`.
- **Evidence:** `docs/sync-protocol.md`; `docs/sync-contract.md`.

### ADR-015 — Guest data is associated with an account only by an explicit step

- **Status:** Accepted
- **Context:** Signing in on a device that already holds guest data should not silently hand that data to an account.
- **Decision:** Creating an account adopts the guest workspace. Signing in to an existing account does not: ownerless records are uploaded only after `associate_local_data()`. Each view shows one workspace at a time.
- **Rationale:** Stated in `docs/sync-protocol.md` "Accounts, ownership, and association".
- **Alternatives:** Not documented.
- **Consequences:** The desktop has no view of other workspaces.
- **Relevant files:** `app/sync/service.py`, `app/sync/engine.py`, `app/ui/app_services.py`.
- **Evidence:** `docs/sync-protocol.md`; `docs/desktop-web-boundaries.md` section 8.

### ADR-016 — Every server write goes through one Mutator

- **Status:** Accepted
- **Context:** REST CRUD, the planning API and sync push must apply the same rules.
- **Decision:** `backend/mutations.py: Mutator` handles user scoping, preconditions, relationship rules, server versions and the change log for every accepted change.
- **Rationale:** The docstring calls it "The one server mutation path"; the sync docs say pushes are applied "through the same Mutator as the REST API".
- **Alternatives:** Not documented.
- **Consequences:** Adding a write route means going through the Mutator.
- **Relevant files:** `backend/mutations.py`, `backend/api.py`, `backend/sync.py`, `backend/planning_repository.py`.
- **Evidence:** The `mutations.py` docstring; `docs/sync-protocol.md`.

### ADR-017 — Server backend on FastAPI with PostgreSQL, SQLAlchemy 2 and Alembic

- **Status:** Accepted
- **Context:** Accounts and multi-device data need a server.
- **Decision:** FastAPI with PostgreSQL (psycopg 3) through SQLAlchemy 2, with versioned Alembic migrations. SQLite is used only so the ordinary test suite needs no server.
- **Rationale:** The library choice is stated in the `backend/__init__.py` docstring, but no reason for it is recorded. The SQLite-for-tests reason is in `backend/database.py`.
- **Alternatives:** Not documented.
- **Consequences:** PostgreSQL-specific behavior is checked only by `postgres`-marked tests and the `postgres` CI job.
- **Relevant files:** `backend/app.py`, `backend/database.py`, `backend/models.py`, `backend/migrate.py`.
- **Evidence:** Module docstrings; `docs/backend.md`.

### ADR-018 — Normalized server storage with immutable revisions, via expand/backfill/contract

- **Status:** Accepted
- **Context:** Revision 0003 stored structured content as JSON columns.
- **Decision:** Revisions 0004–0006 moved that content into relational child and revision tables using expand, backfill and contract steps. The upgrade aborts if any value cannot be converted exactly. Recorded sync outcomes reference immutable revision snapshots.
- **Rationale:** The migration procedure is documented: no lossy conversion, bounded memory, and conversion code that imports nothing from the application. Why normalize at all is not recorded.
- **Alternatives:** Keep JSON columns (the previous state).
- **Consequences:** Every new field needs a live column, a revision column and a mapping in `record_mapping.py`.
- **Relevant files:** `backend/models.py`, `backend/record_mapping.py`, `backend/snapshots.py`, `backend/migrations/normalized_storage.py`.
- **Evidence:** `docs/backend.md` "Normalized storage (0004-0006)".

### ADR-019 — Authentication: Argon2id, short JWT access tokens, rotating refresh credentials

- **Status:** Accepted
- **Context:** Native clients and browsers both need to authenticate.
- **Decision:**
  - Passwords are hashed with Argon2id, with bounded concurrent hashing work.
  - Access tokens are HS256 JWTs.
  - Native clients get rotating refresh credentials (stored as SHA-256 digests) with reuse detection.
  - Browsers get session cookies with CSRF protection.
  - Optional TOTP MFA, email verification, recovery and session revocation.
- **Rationale:** `security.py` says it is built "only on maintained libraries". Each mechanism's rules are in its module docstring and `docs/account-security.md`.
- **Alternatives:** Not documented.
- **Consequences:** The server needs `JWT_SECRET` and an MFA encryption key. The user identity comes only from `current_user_id`.
- **Relevant files:** `backend/passwords.py`, `security.py`, `native_sessions.py`, `browser_sessions.py`, `mfa.py`, `api.py`.
- **Evidence:** `docs/account-security.md`, `docs/api-security.md`, `docs/backend-m8.md`; commit `447079c`.

### ADR-020 — Access tokens stay in memory; the saved refresh credential goes in the OS vault

- **Status:** Accepted
- **Context:** The desktop must not leave credentials in files or logs.
- **Decision:** The password is sent once and never stored. The access token lives only in `SyncService` memory. "Keep me signed in" stores the refresh credential through `keyring`.
- **Rationale:** Stated in `docs/sync-protocol.md` "Credentials".
- **Alternatives:** Not documented.
- **Consequences:** Without a credential store, the app falls back to `MemoryVault` and the session does not survive a restart.
- **Relevant files:** `app/sync/credentials.py`, `app/sync/service.py`, `app/logging_setup.py` (log scrubbing).
- **Evidence:** `docs/sync-protocol.md`.

### ADR-021 — Rate-limit counters live in the database

- **Status:** Accepted
- **Context:** Throttling has to work across several workers and replicas.
- **Decision:** Sliding-window counters are kept in `rate_limit_buckets`.
- **Rationale:** The docstring says "a per-process counter would multiply the limit by the number of workers".
- **Alternatives:** In-process counters (rejected for that reason).
- **Consequences:** Rate limiting adds database writes on authentication routes.
- **Relevant files:** `backend/rate_limit.py`, `backend/login_guard.py`.
- **Evidence:** The `backend/rate_limit.py` docstring.

### ADR-022 — Email goes through a durable outbox; HTTPS providers are supported

- **Status:** Accepted
- **Context:** A request should not depend on the mail provider being up, and Render blocks outgoing SMTP.
- **Decision:** Requests write to `email_outbox` and a background worker sends the mail. Providers: SMTP, Resend, Brevo and Mailjet.
- **Rationale:** The outbox docstring says "A request never talks to the mail provider". Commit `a3e9ff1` says the HTTPS providers were added because "Render blocks outgoing SMTP".
- **Alternatives:** SMTP only (insufficient on Render).
- **Consequences:** Delivery is asynchronous; webhooks report bounces and complaints.
- **Relevant files:** `backend/outbox.py`, `backend/mail.py`, `backend/email_webhooks.py`.
- **Evidence:** Module docstrings; commit `a3e9ff1`; `docs/email.md`.

### ADR-023 — Password policy: at least 8 characters and common passwords refused

- **Status:** Accepted
- **Context:** Signup, recovery reset and password change all need one consistent rule.
- **Decision:** `check_new_password` is the only rule for new passwords. The minimum length is 8, common, repetitive and sequential passwords are refused, and an optional breach check is available (`PASSWORD_BREACH_CHECK`).
- **Rationale:** Commit `0886385` ("Lower the minimum password length to 8; refuse more common passwords"). The reason for the lower minimum is not recorded beyond that message.
- **Alternatives:** A longer minimum (used before that commit).
- **Consequences:** None recorded.
- **Relevant files:** `backend/password_policy.py`, `backend/accounts.py`.
- **Evidence:** Commit `0886385`; the module docstring.

### ADR-024 — Direct PostgreSQL storage is a development-only mode

- **Status:** Accepted
- **Context:** Using the server database straight from the desktop is convenient during development but needs database credentials.
- **Decision:** Only `python -m app.app --storage postgres` offers this mode. `app/desktop.py` ignores it, and it has no local replica and no sync.
- **Rationale:** The `desktop.py` docstring says "a distributed client must never hold" database credentials.
- **Alternatives:** Not documented.
- **Consequences:** It needs its own dependency set (`requirements-direct.txt`) and the `direct-desktop` CI job.
- **Relevant files:** `app/persistence/`, `app/ui/direct_services.py`, `app/desktop.py`.
- **Evidence:** The `app/desktop.py` docstring; `docs/direct-postgres.md`.

### ADR-025 — All desktop storage work runs off the Tk thread on a bounded worker pool

- **Status:** Accepted
- **Context:** Storage and scheduling calls froze the UI.
- **Decision:** `WorkerRegistry`:
  - at most 4 threads;
  - a serial `WRITE_LANE`, so mutations never interleave;
  - results are delivered by a main-thread poll;
  - stale results (from another workspace or a destroyed page) are dropped;
  - workers never call Tk.
- **Rationale:** The docstring explains the number 4: storage is one SQLite connection behind one lock, so more threads would only wait on that lock while competing with the Tk thread.
- **Alternatives:** Unbounded threads (rejected by that reasoning).
- **Consequences:** Every page must make its storage calls through `run_io(..., background=True)`.
- **Relevant files:** `app/ui/background.py`; `docs/desktop-responsiveness-*.md`.
- **Evidence:** The `background.py` docstring; the responsiveness reports.

### ADR-026 — Cyclic garbage collection runs on the Tk thread while windows are alive

- **Status:** Accepted
- **Context:** Tcl objects freed by garbage collection on a worker thread are unsafe.
- **Decision:** While a desktop window exists, automatic cyclic GC is turned off and replaced by a main-thread timer. The original setting is restored afterwards.
- **Rationale:** The `DesktopCollection` docstring: "collect cycles on its thread rather than on sync workers".
- **Alternatives:** Not documented.
- **Consequences:** None recorded.
- **Relevant files:** `app/ui/tk_lifecycle.py`.
- **Evidence:** The module docstring.

### ADR-027 — Controllers return ControllerResult instead of raising into Tk

- **Status:** Accepted
- **Context:** An exception in a Tk callback can break the UI.
- **Decision:** Controller methods that can fail return `ControllerResult` (`.ok`, `.error`). Views stay thin and controllers are Tk-free, so they can be tested without a display.
- **Rationale:** The `background.py` docstring calls it the structured error-handling mechanism, so that "an exception [never] reach[es] -- and potentially crash[es] -- the UI".
- **Alternatives:** Not documented.
- **Consequences:** Most UI logic can be tested without a display.
- **Relevant files:** `app/ui/background.py`, `app/ui/*_controller.py`.
- **Evidence:** The module docstring; `docs/desktop-web-boundaries.md`.

### ADR-028 — `app/runtime.py` is the only source-versus-packaged switch

- **Status:** Accepted
- **Context:** Paths differ between a source checkout and a PyInstaller build.
- **Decision:** Only `runtime.py` checks whether the app is frozen. Bundled resources are read through `resource_path` and are read-only; writes go under `data_dir()`.
- **Rationale:** The module docstring says it is "the one module that knows the difference".
- **Alternatives:** Not documented.
- **Consequences:** None recorded.
- **Relevant files:** `app/runtime.py`, `config/settings.py`.
- **Evidence:** The module docstring; the `.claude/skills/schedule-maxing` skill.

### ADR-029 — The version is written once, and the release tag must match it

- **Status:** Accepted
- **Context:** The About page, executable metadata, installer and updater all need the version.
- **Decision:** `app/version.py` holds `__version__`. The release workflow checks that the tag equals `v` plus that version.
- **Rationale:** The module docstring: "The one place the application's version is written".
- **Alternatives:** Not documented.
- **Consequences:** None recorded.
- **Relevant files:** `app/version.py`, `packaging/windows/version_info.py`, `.github/workflows/release.yml`.
- **Evidence:** The docstring; `docs/ci-cd.md`.

### ADR-030 — Windows delivery: PyInstaller folder build, Inno Setup, signing optional

- **Status:** Accepted
- **Context:** The app needs a distributable Windows installer.
- **Decision:**
  - PyInstaller folder build: windowed, no UPX.
  - The bundle is checked (`check_bundle.py`) and a packaged self-test is run.
  - Inno Setup 6 builds the installer, and SHA256SUMS are published.
  - Code signing happens only when a certificate is configured.
- **Rationale:** The procedure is documented. Why a folder build rather than a one-file build is not recorded.
- **Alternatives:** Not documented.
- **Consequences:** Unsigned builds trigger SmartScreen. Releases are Windows-only.
- **Relevant files:** `packaging/windows/`, `app/selftest.py`.
- **Evidence:** `docs/windows-distribution.md`.

### ADR-031 — Updater trusts GitHub over HTTPS plus the published SHA-256

- **Status:** Accepted
- **Context:** Installed copies must update safely.
- **Decision:** The updater reads the latest stable GitHub release, downloads the installer to a `.partial` file, checks its size and SHA-256 against `SHA256SUMS.txt`, and only then renames it.
- **Rationale:** The trust model is written out: this protects against corruption and swapping, but not against a malicious build, which needs signatures; `verify_signature` is the hook for that.
- **Alternatives:** Not documented.
- **Consequences:** Releases must be publicly readable.
- **Relevant files:** `app/update/downloader.py`, `release_feed.py`, `transport.py`.
- **Evidence:** The `downloader.py` docstring; `docs/windows-distribution.md`.

### ADR-032 — CI runs once per pull request; deploys and releases reuse that result

- **Status:** Accepted
- **Context:** Running the suite again on merge or tag would cost time without adding coverage.
- **Decision:** `ci.yml` runs on pull requests and manual runs. The deploy and release workflows call `.github/scripts/pr_tests_passed.sh` to require that CI passed for the merged pull request.
- **Rationale:** Stated in the `ci.yml` header comment and `docs/ci-cd.md`.
- **Alternatives:** CI on every push (the earlier state, still described in some docs).
- **Consequences:** A commit pushed straight to `main` is untested; change code through pull requests.
- **Relevant files:** `.github/workflows/`, `.github/scripts/pr_tests_passed.sh`.
- **Evidence:** `ci.yml` comment; `docs/ci-cd.md` section 1.

### ADR-033 — Automatic test tiers; the fast `dev` suite while working; no parallel tests

- **Status:** Accepted
- **Context:** The full suite is large and slow.
- **Decision:** `tests/test_tiers.py` assigns the `unit`, `integration`, `system`, `ui`, `slow` and `dev` markers. Agents run focused tests while working, then `pytest -m dev` and `ruff check .` once; the full suite is for milestones. pytest-xdist and shared windows are deliberately not used.
- **Rationale:** The agent instruction files and `docs/testing.md` "Deliberately not done": Tk needs one main thread, and restart tests must be real restarts.
- **Alternatives:** Parallel runs, a shared app window, a database template (all rejected there).
- **Consequences:** None recorded.
- **Relevant files:** `pyproject.toml`, `tests/test_tiers.py`, `tests/conftest.py`.
- **Evidence:** `docs/testing.md`; `CLAUDE.md`; `AGENTS.md`.

### ADR-034 — One planning time zone; DST-transition and overnight windows are refused

- **Status:** Accepted
- **Context:** Local wall-clock scheduling across DST changes is ambiguous.
- **Decision:** The app plans in one IANA zone (the system's, or `SCHEDULE_MAXING_TIMEZONE`). Ambiguous or nonexistent local times, and day windows that cross a DST transition or midnight, raise explicit errors.
- **Rationale:** `app/planning/time.py` raises `UnsupportedSchedulingWindowError` "rather than silently truncat[ing]" the window.
- **Alternatives:** Not documented.
- **Consequences:** Users must split overnight blocks. This is listed as not implemented in `docs/sync-contract.md`.
- **Relevant files:** `app/planning/time.py`, `app/planning/system_clock.py`, `config/settings.py`.
- **Evidence:** The `time.py` docstring; `docs/desktop-task-form.md` "Limitations".

### ADR-035 — Points replace priority, and task kinds are Flexible, Fixed and To Do

- **Status:** Accepted
- **Context:** In 1.1.0 the product moved toward tracking productivity.
- **Decision:** Every kind carries points (0–1000) that count on completion and never affect placement; the scheduler reads no task priority. The task form has one Flexible/Fixed/To Do switch and no advanced settings; existing deadlines, dependencies and repeat rules are still honored.
- **Rationale:** The README "New in 1.1.0" and the author note: the emphasis moved toward recording tasks and scoring performance.
- **Alternatives:** The earlier priority-driven form.
- **Consequences:** The legacy `priority` fields remain in models for compatibility.
- **Relevant files:** `app/planning/models.py` (`TaskKind`, `points`), `app/ui/task_form_model.py`.
- **Evidence:** README; `docs/desktop-task-form.md`.

### ADR-036 — Analytics are recomputed from records; executions snapshot task fields

- **Status:** Accepted
- **Context:** Points and outcomes must stay correct after edits, reopenings and deletions.
- **Decision:** The tracker recomputes everything from authoritative records, with no stored counter. Executions snapshot name, category and points when they are created.
- **Rationale:** `tracker.py` says it is "computed from authoritative records every time -- there is no stored c[ounter]". `docs/productivity-redesign-plan.md` explains that reopening an execution makes an accumulating counter wrong.
- **Alternatives:** A stored counter (rejected in the plan).
- **Consequences:** Report cost grows with history; ranges are bounded (`docs/analytics.md`).
- **Relevant files:** `app/productivity/tracker.py`, `app/execution/models.py`.
- **Evidence:** The `tracker.py` docstring; `docs/productivity-redesign-plan.md` section 1.

### ADR-037 — The ML duration predictor is evidence-gated and kept out of the UI

- **Status:** Accepted
- **Context:** Personal-scale history is small.
- **Decision:** A scikit-learn model is enabled only above sample thresholds, evaluated against the median predictor on a chronological split, and reachable only through the comparison CLI.
- **Rationale:** The README says it "is expected to stay disabled" on typical data — "by design, not a defect".
- **Alternatives:** Not documented.
- **Consequences:** scikit-learn and pandas are still desktop dependencies.
- **Relevant files:** `app/productivity/ml_*.py`, `predictor_comparison_cli.py`.
- **Evidence:** README "Current Limitations".

### ADR-038 — The local web profile listens on loopback only, behind a session guard

- **Status:** Accepted
- **Context:** A browser UI needs local data, but any web page can make the browser send requests to 127.0.0.1.
- **Decision:** `python -m app.web` binds only to loopback and protects its routes with a local session guard.
- **Rationale:** The `local_session.py` docstring explains the cross-site risk to loopback services.
- **Alternatives:** Not documented.
- **Consequences:** It cannot run at the same time as the desktop on one database (ADR-010).
- **Relevant files:** `app/web/`.
- **Evidence:** Docstrings of `app/web/local_session.py` and `local_app.py`; `docs/web-api.md`.

### ADR-039 — Budget records: a separate domain, integer minor units, expanded recurrence, optional sync feature

- **Status:** Accepted
- **Context:** Users track a balance and recurring income and expenses next to their plans; money must be exact, and budget data must never influence scheduling.
- **Decision:**
  - `app/budget/` is its own domain; the scheduling stack (constraints, PERT, reward, engines, `app/planning/`) never imports it. A budget record may only reference a project.
  - Amounts are integer minor units (2 decimals); floats are refused.
  - Recurring entries are expanded per query; only the entry and per-occurrence edits (keyed by the scheduled date, one live edit each) are stored. A monthly item falls on the month's last day when the month is shorter.
  - Records are sync-ready (ADR-013) in local schema v17 (and v18 for Future items) and server revisions 0019-0020, written on the server through the Mutator (ADR-016). They synchronize only with a server that lists the `budget` feature, and `GET /changes` returns them only with `include_budget=true`; a client that first sees the feature replays the change feed once.
- **Rationale:** Recorded in the docstrings of `app/budget/money.py`, `service.py` and `backend/sync.py` (`SYNC_FEATURES`).
- **Alternatives:** Materializing future occurrences (unbounded rows); float or Decimal-text storage; storing budget entries as tasks (would leak into scheduling).
- **Consequences:** One currency per workspace. Concurrent edits of one occurrence on two devices are an explicit conflict (ADR-014). Wire changes to budget records must be made in client and server together.
- **Relevant files:** `app/budget/`, `app/persistence/budget.py`, `app/execution/db.py` (v17, v18), `backend/resources.py`, `backend/models.py`, `backend/migrations/versions/0019_budget_records.py`, `0020_budget_future_items.py`, `app/sync/mapping.py`, `app/sync/engine.py`.
- **Evidence:** The module docstrings above; `tests/budget/test_budget_service.py` (import separation).

### ADR-040 — Budget forecast: a balance snapshot plus a 15-day one-time spending average; future items are not transactions

- **Status:** Accepted
- **Context:** Budgeting > Stats forecasts the balance for 90 days; Budgeting > Future holds income ideas and wishlist items that must not disturb any figure.
- **Decision:**
  - The entered balance is a snapshot at the end of its as-of date (the desktop saves it as today's); only transactions after it are applied. Every manual change is a new record, so Stats > History can list each one with the balance calculated just before it.
  - Everyday spending is the one-time (non-recurring) expenses of the last 15 calendar days, today included, divided by 15 (days without spending count as zero), subtracted every forecast day; recurring expenses, future one-time expenses and all future income are applied on their own dates. The running balance is an exact `Fraction`; display values round half to even to the cent.
  - Statistics report only the history from the first entry's date to today, never padded or extrapolated.
  - Income ideas and wishlist items are separate record types, never read by occurrences, totals, statistics or the forecast. An idea's progress is the amount received or firmly secured against the expected amount. Turning a wishlist item into an expense creates one one-time expense whose id is derived from the item (`wish_expense_id`) and records it on the item, which then refuses another conversion.
- **Rationale:** Recorded in the docstrings of `app/budget/stats.py`, `app/budget/service.py` and `app/budget/models.py`.
- **Alternatives:** A long-run or weighted spending average; counting recurring expenses in the average (double counting them); treating wishlist items as undated expenses.
- **Consequences:** The forecast has no seasonality. Two devices converting the same wishlist item offline produce one expense record and an explicit conflict (ADR-014), never two expenses.
- **Relevant files:** `app/budget/stats.py`, `app/budget/service.py`, `app/budget/models.py`, `app/ui/budget_controller.py`.
- **Evidence:** `tests/budget/test_budget_stats.py`, `tests/budget/test_budget_future.py`, `tests/sync/test_budget_sync.py`.

### ADR-041 — The background sync cadence follows the desktop window's activity

- **Status:** Accepted
- **Context:** The sync loop ran every 60 s and looked at the pending count every 5 s whether or not anyone used the window; the status bar re-read the sync status every 4 s, also while minimized (docs/desktop-performance-round3.md).
- **Decision:**
  - The desktop reports its window's activity -- focused, unfocused (visible, another application in front) or minimized (iconified) -- from the root's focus and map events (`app/ui/activity.py`, `ACTIVITY_POLICIES`) through the thread-safe `SyncService.set_activity`.
  - Interval: 60 s while focused, 300 s otherwise (`background_interval`). Minimized: no pending-count looks. Regaining focus syncs at once when the last successful sync is older than 60 s.
  - A local commit (a `TransactionState` commit listener) is pushed 2.5 s after the last one (`edit_debounce`); the pending count is skipped when nothing was written since it was last counted (`db.data_version`).
  - The status bar polls every 4 s focused, 15 s unfocused, never while minimized (one read on restore).
  - The first background run waits 3 s after start-up (`start(initial_delay=...)`), so it does not compete with building the window.
- **Rationale:** Near-zero work while the app is not in use, without weakening durability: pending records stay in the durable outbox and the next run pushes them; Sync now and the backoff are unchanged.
- **Alternatives:** A fixed slower interval (later uploads while in use); stopping sync entirely while minimized (pending edits could wait indefinitely).
- **Consequences:** While unfocused or minimized, another device's changes arrive up to 5 minutes later. The sync protocol is unchanged.
- **Relevant files:** `app/sync/service.py`, `app/ui/activity.py`, `app/app.py`, `app/execution/db.py`.
- **Evidence:** `tests/test_sync_activity.py`, `tests/ui/test_activity.py`, `tests/ui/test_activity_window.py`, `tests/sync/test_local_first_accounts.py::test_the_idle_loop_notices_new_pending_work_without_a_request`.

### ADR-042 — Local SQLite keeps the rollback journal: WAL evaluated and not adopted; no new indexes

- **Status:** Accepted
- **Context:** Round 3 evaluated `journal_mode=WAL`, `synchronous=NORMAL`, `cache_size`, `temp_store=MEMORY` and `busy_timeout` on the app's real write and read paths (200 tasks).
- **Decision:** `get_connection` is unchanged: rollback journal, default synchronous, no extra pragmas, and no index added.
- **Rationale:** WAL + NORMAL made a task save about 3x faster (median 3.2 -> 0.95 ms) but every write already runs off the Tk thread, so no UI latency changes; a schedule generation went 621 -> 605 ms. In exchange WAL keeps `-wal`/`-shm` files beside the database, makes the documented restore (copy a backup over `executions.db`) unsafe while a stale `-wal` exists, and makes a plain file copy of a running database miss recent commits. The other pragmas showed no measurable effect (the default 5 s busy timeout already applies). EXPLAIN QUERY PLAN of the Day/Week snapshot reads and the status read showed scans only of the workspace's own tasks, which those reads need in full (the date filter is an expression over preferred dates), and one executions count; a Day load takes about 13 ms in a worker and a status read about 0.05 ms.
- **Alternatives:** WAL with updated restore instructions (revisit if sync applies or imports become write-bound); `CREATE INDEX IF NOT EXISTS` outside the migrations (rejected: indexes belong in versioned migrations, ADR-009).
- **Consequences:** No storage-format change in round 3.
- **Relevant files:** `app/execution/db.py`, `app/execution/backup.py`.
- **Evidence:** docs/desktop-performance-round3.md (SQLite section).

### ADR-043 — Long lists and static text use one-window light widgets

- **Status:** Accepted
- **Context:** Moving the window made dwm.exe use 22-24% of one core on the Day page (200 tasks), against 7-10% for an empty window of the same size. The cost follows the number of visible native child windows: Tk gives every widget one, and a CustomTkinter widget is two or three (frame, drawing canvas, inner label). 400 items as CTk buttons (1203 windows) cost 35%, the same items drawn on one Canvas 5% (docs/desktop-performance-round3.md, "Round 3b").
- **Decision:** `app/ui/light_widgets.py` (`LightFrame`, `LightLabel`, `LightButton`): plain Tk widgets, one native window each, registered with CustomTkinter's appearance and scaling trackers so they follow light/dark and the interface size, with AppButton's keyboard behaviour (Tab focus ring, Enter/Space). They are used for the available-task chips, the status-board cards, `SectionTitle`, the captions/hints/errors of `LabeledEntry`/`LabeledSelect` and `Notice` text. Interactive inputs (entries, menus, primary buttons) stay CustomTkinter.
- **Rationale:** Measured: Day page 556 -> 435 native windows, dwm.exe while moving ~23% -> ~16% of one core; a card is 7 windows instead of 16.
- **Alternatives:** `WS_EX_COMPOSITED` on the window (Tk repaints endlessly with it); drawing every list on one Canvas (the largest saving, but loses per-item focus and widget-level tests; a later option for very long lists); converting every CTk widget (changes the look of the whole app).
- **Consequences:** List buttons have square corners and ghost buttons no outline. A new long list should use the light widgets; a page's native-window count is the measure to watch. Hidden child windows cost as much as visible ones, so the shell keeps only the home page (Day) and the page left last attached while hidden, and detaches every other built hidden page into a withdrawn window of its own (`AppShell._hide`/`_detach`, `wm manage`) -- the page left last too after 30 s hidden -- only when no idle callback is pending (a `wm` command runs them all at once); showing a detached page again costs a re-layout of 0.2-0.7 s. Opening every page left the window with 3,847 native child windows before this bound and 454 after it.
- **Relevant files:** `app/ui/light_widgets.py`, `app/ui/components.py`, `app/ui/day_page.py`, `app/ui/task_status_board.py`.
- **Evidence:** `tests/ui/test_light_widgets.py`; the dwm.exe measurements above.

### ADR-044 — The desktop runs the day engine in a separate process

- **Status:** Accepted
- **Context:** A schedule generation's day engine is CPU-bound Python running in a worker thread; it shares the GIL with the Tk thread.
- **Decision:** `app/planning/engine_process.py`: one spawned worker process (`ProcessPoolExecutor`) runs only the pure engine call (`workflow._generate_date` -> `run_engine(generate_selected_day, ...)`). Reading inputs, reservations, the stale checks and the save stay in the worker thread, which waits for the result. Installed by the desktop's `open_app_services(engine_process=...)` from `settings.GENERATION_PROCESS` (on by default; `SCHEDULE_MAXING_GENERATION_PROCESS=off`), warmed up after start-up, shut down when storage closes. A broken process or an undeliverable answer falls back to the same call in the thread for the rest of the session; an engine error keeps its type.
- **Rationale:** Heartbeat lateness during a 200-task generation: p95 26-41 -> 15 ms. The engine is deterministic (ADR-007), so the process gives the same result, verified by `tests/test_engine_process.py`.
- **Alternatives:** Keep the thread (round 3 measured no stall caused by the engine itself; the worst stall is rendering the result); moving the whole generation (it needs the database connection, ADR-008/010).
- **Consequences:** One extra Python process while the desktop runs. Packaged builds rely on `multiprocessing.freeze_support()` in `packaging/windows/launcher.py`. The test suite turns the process off except in its own tests.
- **Relevant files:** `app/planning/engine_process.py`, `app/planning/workflow.py`, `app/ui/app_services.py`, `config/settings.py`, `packaging/windows/launcher.py`.
- **Evidence:** `tests/test_engine_process.py`; docs/desktop-performance-round3.md ("Round 3b").

### ADR-045 — Light caching: an opt-in mode that keeps only Day and the open section built

- **Status:** Accepted
- **Context:** By default every page stays built once opened (hidden pages are detached, ADR-043), so returning to one is quick, but a session that visited every page holds ~3,200 Tk widgets and ~2,650 native windows.
- **Decision:** Settings -> Appearance -> "Light caching" (`UISettings.light_caching`, off by default, saved in `ui_settings.json`). While it is on, `AppShell` keeps built only the home page (Day) and the pages of the shown page's section (`page_group`, e.g. Settings | Account | How to Use); every other built `DeferredPage` is released (`DeferredPage.release`) once the shell is quiet, its widgets destroyed innermost first in ~12 ms slices, and built anew on its next show (dates come back from `ShellState`). The idle Week pre-build is skipped.
- **Rationale:** Measured after opening every page: 3,246 -> 462 widgets, 2,659 -> 411 native windows, 89 -> 74-78 MB private memory, main-window child windows 673 -> 403, dwm.exe while moving ~10-25% lower (noisy). Switching to Day is unchanged (~50 ms).
- **Alternatives:** Making it the default (rejected: returning to Week/Month costs ~0.8 s instead of ~60 ms, Settings ~0.43 s instead of 0.15 s); releasing in one block (90-350 ms freezes).
- **Consequences:** A trade of memory and window weight for slower returns to other sections. State held only by a page's widgets (scroll position, Productivity's report cache, unsaved form input) is lost when it is released.
- **Relevant files:** `app/ui/shell.py`, `app/ui/pages.py`, `app/ui/ui_settings.py`, `app/app.py`.
- **Evidence:** `tests/ui/test_deferred_pages.py` (light caching tests); docs/desktop-performance-round3.md ("Addendum: light caching").