# Milestone 4: repository audit and Claude Code implementation prompts

Audited on 2026-09-23 at commit `3c3711d`. This document is a preparation deliverable, not an implementation of Milestone 4. It incorporates the requested **Normal / ADHD friendly** dropdown beside **Make Schedule**.

## A. Repository audit

### Conclusion

Milestones 0–3 contain a substantial working foundation. The full existing suite passes in the audit environment, including desktop widget tests. They are **not sufficient to implement the requested web UI without prerequisite work**. The HTTP backend is currently an account, record-storage, and synchronization server; it is not yet a web scheduling application backend.

The audit inspected repository instructions, README, configuration/dependencies/CI, both model families, engine/scoring/constraints, allocation and scheduling orchestration, SQLite repositories and migrations, PostgreSQL models and Alembic migrations, API/authentication/mutation paths, sync transport/store/engine/service, execution/productivity modules, CSV contracts, desktop controllers/widgets, and the corresponding test suites. No application implementation was changed.

### Current architecture and completed capabilities

| Area | Evidence and actual state |
| --- | --- |
| Scheduling baseline | `app/models.py`, `app/optimizer.py`, `app/constraints.py`, `app/pert.py`, `app/reward.py`. Protected legacy Greedy Optimizer v1 remains alongside the canonical engine. Preserve both and their regression/differential tests. |
| Canonical domain | `app/planning/models.py`: UUID Project, Task, FixedBlock, ScheduledTask; real dates, aware timestamps, task/project/dependency references, required/preferred dates, deadlines, recurrence data, estimated minutes, audit/version/owner/tombstone fields. |
| Execution | `app/execution/models.py`, `service.py`, `lifecycle.py`: separate TaskExecution/WorkSession aggregates, planned snapshots, start/pause/resume/complete/skip/cancel, active duration and feedback. Legacy non-UUID execution IDs are deliberately retained with durable UUID wire mappings. |
| Local persistence | `app/execution/db.py` migration chain through schema v5; planning/execution repositories share transactional SQLite storage. Optimistic updates, tombstones, history retention, import rollback, and restart behavior have tests. |
| Cloud persistence | `backend/models.py`, `database.py`, Alembic revisions `0001` and `0002`. SQLAlchemy/PostgreSQL schema, composite owner keys/references, versioned mutations, commit-ordered per-user change feed, durable push idempotency. Ordinary tests use migrated SQLite; live PostgreSQL was unavailable in this audit. |
| Authentication | `backend/api.py`, `security.py`: registration, email/username login, Argon2 password hashing, JWT access tokens, `/me` and profile-name update. No refresh, password-reset, account-deletion, email-verification, or server logout/revocation endpoint. There is no plan/status field. |
| Synchronization | `app/sync/*`, `backend/sync.py`: explicit local association, account/backend-keyed outbox/shadows/cursors/conflicts, bounded retries, transactional change capture, no token persistence, two-device tests. |
| Conflict resolution | Durable conflicts with `accept_remote` and `keep_local`. No generic merge operation. Keep-local is refused for remote tombstones and scope collisions; divergent execution history is restricted. |
| Preferences | `app/planning/preferences.py`: built-in → YAML → user → date resolution, persisted/synced layers, explicit absent/value/null semantics. Engine selection is persisted, not yet exposed by a useful UI. |
| Engines | Exactly `precise_greedy` and `adhd_friendly` in `OptimizerMode`. Normal is minute-precise; ADHD mode places tasks over 30 minutes on quarter-hour starts, while tasks of 30 minutes or less retain minute precision. Durations are not rounded or split. |
| Allocation | `app/planning/allocation.py` assigns task IDs to dates with capacity and reason codes; `service.py` generates a selected day. Real week/month date helpers already exist. Allocation results are held in the controller, not stored as a durable allocation-plan entity. |
| Scheduling service | `app/ui/planning_controller.py` is Tk-free and calls allocation/day services, persists placements and provenance, and recomputes freshness. Browser-accessible scheduling endpoints do not exist. |
| Freshness | `app/planning/provenance.py`: persisted fingerprints and placement digests, including engine/preferences/task/fixed-block/dependency/range inputs; restart and sync tests. Generation records store counts, not the full unscheduled explanations. |
| CSV | Canonical stored-planning format v2 in `csv_export.py`/`csv_canonical.py`: projects, tasks, fixed blocks, placements, identities, relationships, recurrence, versions, timestamps, owner and deletion metadata. It is not a complete backup of preferences, executions, or sync state. Legacy half-hour CSV remains a separate lossy compatibility format. |
| Projects | Local `PlanningService` create/read/update/delete and cloud `/projects` CRUD already exist. Project deletion is refused while live tasks reference it. No archive field/operation. Some older documentation incorrectly describes projects as model-only. |
| Recurrence | Rule data and occurrence-aware rescheduling are preserved. `occurrence.py` distinguishes recurring placements by task/date. There is no recurrence expansion service; do not create a working-looking Repeat control. |
| Productivity | Existing execution analytics, evidence-labelled median duration suggestions, and optional gated ML remain separate from scheduling correctness. No requirement to port the full analytics UI in this milestone. |
| Existing frontend | Only CustomTkinter, principally `app/app.py` and `app/ui/*`. No React/Vite/TypeScript project, package manifest, frontend build, or web component/E2E suite exists. |

### Verified blockers and mismatches

1. **Missing application HTTP operations.** OpenAPI exposes health, auth/profile, record CRUD, execution actions, `/changes`, and `/sync/push`. It has no generate/allocate/effective-preference/freshness/reset/CSV application endpoints. Cloud CRUD cannot call the local SQLite-bound controller directly. `SyncService` and its conflict queue run on the client side; `/sync/push` is not a browser “Sync now” operation.
2. **Existing local reads are device-wide.** Task/range/generation queries do not enforce the active account. `PlanningRepository.get_preference()` prefers any owned layer over an ownerless layer, not the signed-in user's layer. A browser account switch cannot safely reuse those reads unchanged. Filtering cards in React would not isolate scheduling, exports, resets, or dependencies.
3. **Incremental generation is absent.** `generate_day_schedule(previous_result=...)` reuses an ID only if the regenerated interval is unchanged; it does not lock earlier placements. Probe: a saved 09:00 task moved to 09:30, with a new placement ID, after adding a higher-priority task. This violates the requested add-work-without-moving-existing-work behavior.
4. **An unchanged generation is not a no-op.** A second identical `schedule_range()` call increased its GenerationRecord version from 1 to 2. Placement deduplication alone is insufficient for “already current.”
5. **Fixed-block validation is too late/incomplete at writes.** Two identical overlapping `/fixed-blocks` creates both returned HTTP 201. The local create/update service also lacks full effective-window checks; the desktop form performs part of validation. The optimizer eventually rejects invalid blocks, but Add Task must reject them before persistence.
6. **Reset omits date preferences.** `PlanningService.clear_range()` clears placements/generations and optionally tasks/blocks, but leaves the date override. Probe confirmed the override survives. Deleting a dated task can also tombstone its placements outside the range; the reset preview must expose such established cascades or refuse them, never promise a narrower scope than it implements.
7. **Timezone allocation defect.** `_feasible_dates_for_task()` compares deadline UTC dates to local planning dates; repository eligibility predicates also use UTC date substrings. Probe: a 30-minute task with deadline `2026-09-23T08:00:00+09:00`, planning in Asia/Tokyo on September 23 with 00:00–07:00 availability, was incorrectly unallocated. Repair the eligibility path as well as allocation.
8. **Preference controls require a capability map.** `weight_category_bonus` is carried but unused. Canonical adaptation discards legacy task-name weight/window dictionaries. ADHD short-gap bonus fields only affect ADHD mode. The scorer uses the first task tag; preserve all tags without implying all chips currently influence scoring. Minimum gap is a soft fragmentation preference, not a mandatory break.
9. **Browser auth/local-data integration is missing.** Existing tokens are bearer tokens; the local service keeps them in memory. There is no browser session or local HTTP bridge, and the sync transport lacks public registration/profile operations. A hosted browser cannot automatically discover a user's desktop SQLite file.
10. **Presentation limitations remain.** Desktop Month is a rolling 30-day view; form validation still requires half-hour values; fixed categories are stored but rendered generically; engine/date preferences and sync/conflict screens are absent; Reward Config edits the wrong runtime path for canonical scheduling.

Other limits to explain, not expand silently: unsupported overnight/offset-transition scheduling windows, explicit rejection of ambiguous/nonexistent local times, no project archive, no recurrence expansion, no paid-plan infrastructure, and conservative range-wide freshness invalidation. README and some module docstrings contain older claims about legacy scheduling, sample loading, projects, and desktop-only Milestone 4; code/tests take precedence.

### Verification performed

| Check | Result |
| --- | --- |
| Full `python -m pytest -q -ra` under isolated Python 3.12.14 audit dependencies | **1107 passed, 1 skipped, 1 warning**, 98.03 seconds. Includes desktop widget tests; they were not skipped in this run. |
| `python -m ruff check .` | **Passed**. |
| `python -m compileall -q app backend config tests benchmarks` under Python 3.12 | **Passed** across project Python source. |
| FastAPI import/factory, actual Alembic upgrade on disposable SQLite, TestClient startup requests | `/health` 200; `/ready` 200, migration head `0002`; OpenAPI inspected. Registration 201 and authenticated `/me` 200. |
| CLI `--demo` using an in-memory DB and temporary export destinations | **Passed**: 3 tasks, 4 fixed blocks, 3 placements; legacy, exact JSON and canonical CSV exports. |
| Audit probes | Reproduced incremental movement, unchanged-generation version bump, reset override retention, overlapping API writes, and timezone allocation failure described above. |
| PostgreSQL | **Not verified live**: `TEST_DATABASE_URL` unset; Docker daemon unavailable. The existing optional PostgreSQL test module skipped. CI defines a real PostgreSQL job, but its remote result was not inspected. |
| Frontend build/tests/typecheck | **Not applicable yet**: no web frontend exists. No Python typechecker is configured. |

Environment caveats: the default `python` is 3.8 without pytest/Ruff; `.venv` points to a missing Python 3.10 executable. The initial default-interpreter checks failed. Initial `compileall .` also traversed incompatible third-party packages in that broken `.venv` and failed. Verification was recovered with bundled Python 3.12 and the declared requirements installed in a separate temporary folder; neither the user's virtual environment nor dependency declarations were modified. This does not independently establish Python 3.10 compatibility. The passing run emitted a Starlette/httpx deprecation warning, which should be addressed through tested compatible dependency bounds rather than blindly following the warning's suggested package change.

## B. Recommended Milestone 4 architecture

Use a React + TypeScript web frontend under `web/`, built with Vite. Use React Router, TanStack Query, React Hook Form + Zod, date-fns for calendar arithmetic, and Radix primitives wrapped in repository-owned styled components. Use CSS variables for themes/category tokens, Vitest + Testing Library for component/integration tests, and Playwright for realistic browser tests. Pin compatible versions and commit a lockfile; check the Node runtime against the selected Vite version.

Vite provides the production asset build ([official guide](https://vite.dev/guide/build)); TanStack Query handles fetched server state ([official overview](https://tanstack.com/query/latest/docs/framework/react/overview)); Radix provides accessible interaction primitives ([official introduction](https://www.radix-ui.com/primitives/docs/overview/introduction)). Query caching is not a replacement for the existing durable sync protocol.

Two explicit deployment profiles are needed to satisfy both a useful hosted web app and reuse of existing desktop/offline data:

* **Hosted web:** browser → same-origin FastAPI application API → PostgreSQL. Extend the cloud backend with real planning operations around the canonical Python engine. The hosted UI must schedule without a desktop process. With no connection it must honestly disable unpersistable mutations; do not invent browser-local scheduling or sync.
* **Local/offline web:** the same built frontend → loopback FastAPI application service → existing SQLite planning/execution/sync services → optional cloud API. The local Python service keeps functioning without internet. It provides the actual local-association, Sync now, outbox status and conflict UI. A local service is necessary to operate the existing Python/SQLite sync implementation without rewriting it in JavaScript.

Keep one typed application-facing contract with a capability response distinguishing these profiles. Do not require a hosted HTTPS page to call localhost automatically. Existing local users open the local web app to associate/sync; the hosted account then sees those records. Cloud-only users do not get a fake local-data count or conflict queue. Document this deployment boundary in product help and setup instructions.

Extract only the necessary Tk-free orchestration into shared application services. Use storage adapters for SQLite and PostgreSQL, not a second optimizer or an entire rewrite of either persistence layer. Cloud writes retain Mutator version/change-log guarantees. Local writes retain transaction/change-capture guarantees. Both profiles enforce owner scope before loading scheduling inputs, not after calculating output.

Prefer same-origin browser sessions using HttpOnly cookies and CSRF/origin protection. Preserve existing bearer auth for desktop sync. Keep cloud access tokens out of browser storage. Do not add refresh/account-recovery/billing systems just to complete this UI milestone.

Allocation previews may be recomputed by the backend from persisted inputs, with a fingerprint checked before selected-day generation; there is no need for a new durable allocation-plan domain unless implementation requires it. Never rely on a singleton controller's last in-memory allocation across HTTP users or workers. Freshness remains a backend classification of persisted provenance.

## C. Implementation sequence

Execute strictly in this order. Finish verification before starting the next prompt.

| Prompt | Deliverable | Dependency |
| --- | --- | --- |
| 0A | Preflight domain repairs | Existing repository |
| 0B | Shared scheduling workflow and hosted application API | 0A |
| 0C | Local web API, account isolation, sync bridge | 0B |
| 1 | Web foundation and design system | 0A–0C |
| 2 | Account, association, synchronization, conflicts | 1 |
| 3 | Complete Day Schedule and engine dropdown | 2 |
| 4 | Week and real Month | 3 |
| 5 | Allocation Planning and Projects | 4 |
| 6 | Settings, engine preference alignment, Account, About | 5 |
| 7 | Full integration, browser E2E, production build and docs | All prior prompts |

The preflight is split into three bounded prompts because domain correctness, hosted scheduling, and access to existing local sync are separate prerequisites. Treating them as frontend polish would hide the largest missing integration.

## D. Full Claude Code prompts

Each block below is independently copyable. The file paths identify the audited implementation; inspect their current contents before editing.

### Prompt 0A — Milestone 4 preflight domain repairs

```text
Prepare the Schedule Maxing repository for Milestone 4's production web UI. Implement only the domain repairs in this prompt; no frontend yet.

First read AGENTS.md, CLAUDE.md, README.md, docs/backend.md, docs/sync-contract.md, docs/sync-protocol.md, and current code/tests. Inspect all prior implementation rather than assuming earlier prompts were completed. The current repository is authoritative. Preserve working CLI, desktop, sync, CSV, identities and Greedy Optimizer v1. Avoid unrelated refactoring. Add meaningful tests, run relevant and full Python checks, fix regressions caused by this work, and update usage/contract documentation. At completion report files changed, architecture decisions, tests added, exact commands/results, and remaining limitations.

Inspect app/planning/application.py, repository.py, allocation.py, time.py, preferences.py, app/ui/planning_controller.py, schedule_page_controller.py, backend/resources.py, mutations.py, and related planning/backend/sync tests.

Repair these audited problems:

1. Fixed-block writes currently permit invalid scheduling inputs. Two overlapping POST /fixed-blocks requests both return 201. Put reusable validation at the application/domain write boundary: positive interval, minute precision, date/timezone consistency, effective date window and no other fixed-block overlap. Apply to create and edit, excluding the edited record itself. Validation and persistence must be atomic, including concurrent writes. Ensure local operations, cloud REST and sync mutations cannot bypass the applicable invariant. Reuse time/constraint logic; do not bury validation in reward scoring. Resolve effective preferences with the same built-in/template/user/date rules that scheduling will use. Preserve supported historical import/sync semantics explicitly; do not silently round or rewrite old data. Add only dependencies actually needed by server preference resolution.

2. Deadline filtering uses UTC dates where planning dates are local. Reproduce a 30-minute task due 2026-09-23T08:00:00+09:00, Asia/Tokyo planning date September 23, available 00:00–07:00: it is incorrectly unallocated. Fix both range eligibility in repository/service and allocation feasibility. Use aware instants and each date's planning timezone; keep the intraday deadline constraint authoritative. Test positive and negative offsets, local/UTC date boundaries, deadline equality, and unsupported DST windows without adding unsupported scheduling behavior.

3. Provide an explicit transactional reset operation for the new web workflow: clear the selected date/range's appropriate tasks, blocks, placements, generation records and date preference overrides; retain user defaults and execution history. Preserve existing reset APIs where callers rely on their older scopes. Return a preview/count of affected records and actual cascades. Do not delete undated backlog or unrelated projects. A dated-task deletion may affect placements outside the range: either reject that reset scope with an explanation or disclose and require confirmation of the cascade. Protect recurring occurrences; do not silently delete the template to remove one date. An external dependency refusal must roll back the entire reset, including preference deletion. Respect versions and sync tombstones.

4. Add request-scoped owner filtering support to local service/repository operations needed by web scheduling: tasks, projects, references, fixed blocks, placements, preferences, generation records, executions, reset and CSV. The existing device-wide behavior must not be exposed to an authenticated web request. Keep compatibility for legacy callers if necessary, but require explicit scope for new web use. Ownerless local data is a separate scope, not automatically visible/claimed by a signed-in account. Preferences and freshness must use the same scope as tasks. Test account A/B/ownerless isolation at the service layer, not just the UI.

Acceptance: invalid fixed-block writes leave DB/change capture unchanged; valid adjacent blocks and 10:13 boundaries succeed; the Tokyo deadline case allocates; reset restores inherited date preferences atomically; out-of-scope reads/writes/exports/reset do not leak or mutate records. Existing identities, recurrence data, execution history, CSV format and optimizer regression behavior remain intact.

Run focused planning/backend/sync tests, then python -m pytest, python -m compileall . (or a documented source-only equivalent excluding virtual environments), and python -m ruff check . using a supported interpreter. Run existing real-PostgreSQL tests if a disposable test server is available; state clearly if not. Do not claim SQLite verifies PostgreSQL locking.
```

### Prompt 0B — Real scheduling application API for hosted web

```text
Implement the hosted application API needed by Milestone 4, after Prompt 0A. No frontend in this prompt.

Read AGENTS.md, CLAUDE.md, README.md and relevant code/tests/docs first. Inspect Prompt 0A's actual changes; never infer completion from its request. Preserve working functionality and use current code as truth. Implement only this prompt, add/update tests, run appropriate focused/full checks, fix regressions, update usage docs, avoid unrelated refactoring, and report files, architecture choices, tests, commands/results and limitations.

Inspect app/planning/{application,service,allocation,preferences,provenance,occurrence,external_dependencies,csv_canonical,csv_export}.py, app/ui/planning_controller.py, app/optimizer.py, and backend/{app,api,resources,mutations,models,sync}.py. The current FastAPI app has storage CRUD but no scheduling API. The controller is SQLite-bound and its current allocation is process memory; do not make it a global HTTP singleton.

Create a small shared, Tk-free application orchestration boundary and a PostgreSQL adapter. Reuse the canonical allocator, day engine, preference resolution, occurrence rules and provenance recipe. Preserve existing controller/import APIs with wrappers if extraction is needed. Do not reimplement scheduling in JavaScript or copy a second optimizer. Do not open a cloud user's data through a shared local SQLite controller.

Expose typed, documented HTTP operations for: a bounded date-range/day snapshot with tasks/blocks/placements/freshness; effective and inherited preferences plus editable layers and engine capabilities; allocation preview with assignments/unallocated reasons/capacity/fingerprint; selected-day generation from a current allocation/range; transactional reset preview/commit; canonical v2 CSV preview/import/export. Reuse existing project/task CRUD. Include pagination or bounded-range behavior and a capability response. Publish concrete request/response schemas in OpenAPI and docs/web-api.md; generic untyped dict pages must not force the frontend to guess DTO shapes.

Generation requirements:
- First run calls the real engine and saves placements plus provenance atomically through the server mutation/change-log path.
- If persisted inputs and placements are current, return already_current without changing IDs, versions, timestamps, provenance, or sync events.
- Existing previous_result only reuses identical IDs; it does not lock work. Add an explicit opt-in canonical incremental operation that reserves valid committed placements and schedules newly eligible work around them. Preserve IDs/intervals and execution links for retained placements. Do not persist fake fixed blocks. Keep the legacy optimizer and canonical full-generation default behavior unchanged; characterize them before extending this path.
- When an edit/engine/window change makes retained placements incompatible, report the reason and require an explicit regenerate action; do not silently move protected work. Explicit regeneration must respect execution-history and occurrence rules.
- A stale input snapshot or concurrent task/preference/block/placement change must fail before commit and preserve the old schedule. Placement versions alone are insufficient: compare the full input fingerprint/read set inside the commit transaction.
- Do not duplicate task occurrences, do not clear a valid schedule when there is no new eligible work/free capacity, and return useful mandatory/optional failure explanations.
- Allocation preview never generates intraday placements. Use persisted inputs plus a checked fingerprint, not an allocation held only in one process. Recompute on reload as appropriate; do not invent a persistent plan table without a concrete need.
- Return backend-derived stale/current state and reasons; preserve empty-generation provenance. Regained reads after restart must not fabricate missing unscheduled explanations from a saved count.

The server derives owner identity from authentication. All reads, dependency resolution, preference queries, exports, and writes are owner-scoped. CSV uploads preserve the established v2 contract and concurrency rules, never trust owner/version fields to bypass authorization. Ownerless-data association is a separate explicit operation; reject mismatches in normal import. Validate a complete batch before applying any of it. Browser upload/download uses bytes/streams, never arbitrary server filesystem paths.

Add browser session support compatible with existing bearer-token sync: same-origin HttpOnly cookies, CSRF/origin validation, expiry, actual logout invalidation for browser sessions, and clear auth errors. Keep passwords and bearer tokens out of browser local/session storage, logs and URLs. Do not build billing, password recovery or token-refresh features in this scope. Add minimal tested dependency/version bounds required by the new server runtime, not desktop/ML packages.

Acceptance tests use real application endpoints: create inputs -> generate -> reload/restart -> identical generate no-op; add high-priority work without moving old placements; explicit regeneration; invalid retained placement refusal; concurrent input changes roll back; both engines; scope isolation; reset; CSV rollback/identity round trip; browser login/logout/expiry/CSRF and existing bearer sync compatibility. Verify generation in a backend-only environment with no Tk/pandas/sklearn import requirement. Run full Python tests/lint/compile and disposable PostgreSQL verification when available. Document actual start commands, API contracts and any remaining blockers before moving on.
```

### Prompt 0C — Local web API and existing sync integration

```text
Implement the local/offline web application service after Prompts 0A and 0B. This exposes existing SQLite data and the established Python sync service to the same forthcoming frontend. The hosted web profile must also remain functional without this local process.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md and sync/backend documentation. Inspect actual prior-prompt changes and relevant files/tests before editing; use the repository as truth. Preserve functioning desktop/CLI/cloud behavior, keep this scope focused, add/update tests, run relevant/full checks, fix introduced regressions, update setup docs, and report files, decisions, tests, commands/results, limitations.

Inspect app/ui/app_services.py, app/ui/planning_controller.py, app/planning/application.py/repository.py, app/execution/db.py/service.py, app/sync/{service,engine,store,transport,mapping}.py and tests/sync. Do not import or launch CustomTkinter to serve web requests. Reuse Prompt 0B's shared planning operation/DTO contracts through a SQLite adapter rather than copying endpoints' business logic.

Create a loopback-only FastAPI factory and documented launcher with explicit DB-path/data-directory and timezone configuration, clean startup migrations, injected test dependencies, and shutdown that waits for work before closing SQLite. It must run offline and serve production frontend assets once they exist. Do not expose a device's SQLite/sync session publicly or bind all interfaces by default.

Implement application capabilities plus local session, backend configuration/connectivity, registration, sign-in/profile/sign-out, local-data association preview/confirm, sync status, Sync now, conflict list/detail/resolution. Extend the transport with public account operations rather than reaching into private methods from route handlers. Cloud tokens remain in server-process memory; the browser gets a protected local session, not the bearer token. Protect loopback endpoints with a session/bootstrap design, trusted Host/Origin checks and CSRF controls; do not make unauthenticated arbitrary websites able to reset local data. Browser auth sessions and background work must not share mutable current-account state unsafely.

Scope each local request before reading, generating, exporting or mutating. Account A, account B, and ownerless local workspace are separate. A signed-out local workspace shows ownerless data only. Bind work/jobs to the originating account/backend/scope; account switching during sync/generation cannot apply results to the new account. Handle legacy mixed-owner stores and backend/identity collisions explicitly without overwriting records or reminting IDs. Keep existing sync account keys/cursors/outbox guarantees.

Sign-in alone must never associate or upload ownerless data. Preview counts by entity type, show ownership/collision problems, and confirm exactly the previewed scope before calling association. If data changed since preview, refresh/reconfirm. Cancelling leaves owners, versions and outbox unchanged. Allow signed-in users to create their own account-owned new records without implicitly claiming old ownerless records; distinguish this from the existing association activation rule and test it.

Sync status must distinguish backend reachability from browser network hints and local API availability. Expose pending dirty/outbox counts without double counting, in-progress state, conflict count, last successful sync (persist it if necessary), last error and auth-required state. Delegate pushing, pulling, retry and conflict decisions to SyncService. No second browser sync engine. Only expose supported accept_remote/keep_local resolutions, with allowed-action/reason metadata; remote deletion and divergent execution history cannot offer a misleading merge/revival action.

A hosted page cannot silently discover the desktop database. Document opening the local web profile to associate/sync it, then opening hosted web to see the uploaded records. No automatic cross-origin localhost probing. Hosted capability output must honestly distinguish direct server persistence from this device's pending sync; it must not fabricate local counts.

Test offline create/generate/restart, account A/B/ownerless isolation for every operation family, registration/login/profile/logout/401, association cancellation/confirmation/race, pending-count persistence, two devices, lost-response replay, live edits during sync, tombstones, supported conflict decisions, account switch during jobs, invalid backend URL, shutdown and loopback protection. Reuse in-process backend/failure transports and temporary SQLite databases; normal tests need no external service. Run full Python tests, lint, compile and applicable PostgreSQL tests. Document both deployment profiles and exact API/launch contracts for the frontend prompts.
```

### Prompt 1 — Web foundation and design system

```text
Implement Milestone 4's web foundation after Prompts 0A–0C. Build in web/; preserve the existing desktop app and backend workflows.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md, and relevant backend/shared-service code/tests. Inspect all actual prior-prompt implementation; do not assume a feature exists because it was requested. Use current contracts as truth. Implement only this scope, add/update tests, run checks, fix regressions, update usage docs, avoid unrelated refactors, and report files, architecture decisions, tests, exact commands/results and limitations.

There was no web frontend at audit time. If one now exists, extend its established conventions instead of scaffolding a duplicate. Otherwise use React, TypeScript, Vite, React Router, TanStack Query, React Hook Form, Zod, date-fns and Radix primitives with repository-owned styling. Select compatible versions from official documentation, record Node requirements, commit a lockfile and define dev/build/test/lint/typecheck scripts. Use Vitest and Testing Library; set up Playwright for later E2E. Add only justified packages.

Build a typed application API client from the now-existing OpenAPI/DTO contracts. Centralize HTTP requests, error envelopes, credentials, CSRF, pagination, cancellation and query keys. Account/backend/workspace identity belongs in cache keys; clear/cancel previous-account data on logout/switch. Keep server data in Query and temporary form/dialog/navigation state separate. Never treat query cache or mock fixtures as persistence. Unknown API failures must become recoverable user messages rather than raw stack traces. Production must not load fixture data or interceptors.

Provide routes/navigation for Day Schedule (default), Week Schedule, Month Schedule, Project Schedule, Settings, Account and About, with an Allocation Planning entry reachable from relevant planning pages. Sidebar starts collapsed, toggles with a labelled hamburger, animates smoothly and becomes an overlay at small widths. Use CSS grid/flex layout; resizing must not trigger render/measurement loops or shaking. Include accessible loading/error/empty-page scaffolds, route errors and authentication/local-workspace capability handling. Temporary page scaffolds are acceptable in this prompt only.

Create reusable buttons/selects/fields, dialogs/drawers, menus, alerts/toasts, confirmation, cards, tabs and focus styles. Use muted category colors, soft borders, modest shadows, rounded blocks and consistent spacing/type tokens. Central category-to-color mapping must support unknown imported categories without changing their data. Text/icons supplement color. Implement genuine light and dark palettes and durable appearance preference without persisting credentials. Use system-independent explicit light/dark choices required by the brief.

Add reusable date and time presentation helpers: date-only values stay date-only; instants use the chosen planning IANA zone. Use h:mm AM/PM for display, preserve minute precision and distinguish next midnight. Python remains authoritative for ambiguous/nonexistent times and scheduling constraints.

Configure development proxy and production same-origin asset serving/reverse-proxy behavior for both hosted and local profiles. Never use the Vite development/preview server as the production server. SPA deep links must return the app; /api errors must not return index.html. Secrets never enter frontend build variables.

Acceptance: fresh install builds and typechecks; every route opens; default Day route and collapsed sidebar work; keyboard/focus/dialog behavior works; light/dark survives reload; representative 390/768/1280/1920px resizing is stable; account switching cannot show cached prior-account data; capability/API failures have real states. Component tests cover these behaviors and category/time helpers. Run npm ci, npm run test -- --run, npm run lint, npm run typecheck and npm run build (adjust only to the documented chosen runner), plus Python checks for backend changes. Update README with exact commands.
```

### Prompt 2 — Accounts, local-data connection, synchronization and conflicts

```text
Implement the real account and sync UI after the web foundation. Use existing application endpoints and SyncService integration; no fake authentication or frontend sync algorithm.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md, sync docs and relevant frontend/backend/tests. Inspect all prior prompts' actual changes and use current code as truth. Preserve functioning features. Implement only this prompt, add/update tests, run relevant checks, fix regressions, update documentation, avoid unrelated refactoring, and report files, architecture choices, tests, commands/results and limitations.

Build registration, login, logout, active-account identity and profile loading with validation, field errors, loading/disabled states, retry and session-expiry handling. Follow backend email/username/password contracts. Passwords and access tokens must not be saved to browser storage or URLs. Logout invalidates the browser session, clears account query state, and leaves persisted planning data intact. Handle both authenticated hosted use and explicit ownerless local/offline workspace use; do not force an online login to use the local profile.

Offer backend connection configuration only where the local service supports it. Validate/persist nonsecret settings through the service, show connectivity status, and safely end the prior connection/session before switching backends. Do not expose arbitrary backend switching for a fixed hosted deployment.

After local sign-in, show the service's unassociated-data preview when such data exists. Explain record counts, owner/association scope and conflicts. Provide separate confirm and cancel actions. Login, opening the dialog, or dismissing it must not mutate ownership or upload data. Confirm calls the actual association endpoint; stale previews must refresh. Existing records owned by another account never become claimable.

Display compact sync state: connectivity, pending count when available, last successful sync, running status, conflicts and useful error/auth-required text. Add Sync now with duplicate-request prevention. Refresh affected queries after synchronization. Durable state comes from the service, not component memory. Distinguish a stopped local API from an unavailable remote backend; local offline editing must still work in the latter case.

Provide a conflict list and accessible detail view. Show labelled local/remote values, modification/version information when available, deletion markers and service reason. Present only allowed Keep local and Accept remote actions; no Merge unless the service truly supports it. Explain why Keep local is unavailable for a remote tombstone or scope collision. Protect current edits, provide feedback/retry, and refresh the conflict and affected schedule/freshness queries after resolution.

In hosted-only mode, show actual server persistence/connectivity and guidance for connecting existing local data through the local web app. Do not show imaginary local pending changes or label a simple query refetch as synchronization. Keep the local sync workflow readily discoverable and documented.

Acceptance: registration/login/profile/logout work against real endpoints; expiry clears stale identity without deleting data; account A/B data never flashes across switching; cancelling association changes nothing; confirmed association preserves IDs and syncs through the existing service; offline edits survive local restart; conflict decisions use allowed service operations and persist. Test duplicate submissions, backend 401/409/422/5xx, network failure, account switch during requests, deleted conflicts and successful retry. Use component/integration tests plus in-process backend tests, not external production services. Run frontend test/lint/typecheck/build and relevant Python tests; update usage docs.
```

### Prompt 3 — Complete Day Schedule, preferences and engine dropdown

```text
Implement the main Day Schedule experience after the account/sync UI. This must use persisted application APIs and the repaired real generation service.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md and relevant current models/services/frontend/tests. Inspect actual results of every prior prompt rather than assuming completion. Preserve working behavior and use current code as truth. Limit work to this prompt, add/update tests, run relevant/full checks, fix introduced regressions, update docs, avoid unrelated refactoring, and report files, architecture decisions, tests, commands/results and limitations.

Open today's real date in the chosen planning timezone; allow date navigation and retain Week/Month return context. Load persisted tasks, fixed blocks, placements, preferences and freshness from the API. Never fabricate numeric day identities or derive currentness from component state.

Upper workspace: horizontal minute-proportional schedule timeline with hour labels and exact h:mm AM/PM intervals. Fixed blocks appear immediately; flexible placements appear after generation. Use actual category colors for both. Show available/unplaced tasks in a bounded scroll/wrap region with identity-based actions. Compute each Free time gap inside the effective day window for display only; never persist fake tasks. Expose keyboard-accessible edit/delete menus and clear unscheduled explanations. Duplicate names must remain distinguishable by identity/context.

Lower workspace: reusable Add/Edit Task form plus action area. Derive fields from canonical models: name/category, duration in minutes, priority 1–10, required flag/date, preferred dates/window, deadline, dependencies and project where relevant. Distinguish a flexible task from a FixedBlock: blocks have label/category/date/start/end, not fake flexible task fields. Hide generated UUID/owner/version/audit fields. Category/project/type use selects, preserving unknown imported categories. Use a validated time control with both typing and picker/scroll interactions, one-minute values, h:mm AM/PM display and correct noon/midnight handling. Do not reuse desktop half-hour form validation.

Tags: Enter creates a chip without submitting the form; chips can be removed and wrap/scroll. Preserve the canonical ordered string array. Preserve unsupported editable data such as recurrence when editing other fields; no fake Repeat control. Explain only as needed that current tag scoring uses the first tag, without changing the scorer. Dependencies use IDs and meaningful labels.

Fixed-block errors must come from authoritative save validation: overlap, outside effective window, start/end order, invalid date/timezone. Keep entered form values after failure and do not show a saved card until persistence succeeds. Optimistic updates are unsuitable for generation/reset or conflict-prone schedule edits.

Add Day Preferences drawer using the real effective/inherited/layer API. Show inherited versus overridden fields, save sparse date overrides, reset one override or the whole date to inheritance, and preserve absent/null semantics. Share controls with future Settings. Expose only active-engine capabilities; no YAML editor, inactive category-bonus control, legacy task-name config or simulated-annealing fields. A spacing preference must not be described as a guaranteed break.

Place an accessible dropdown labelled Engine immediately beside Make Schedule. Options exactly:
  Normal -> precise_greedy
  ADHD friendly -> adhd_friendly
Normal is the fallback when no persisted preference overrides it. Display the effective saved date mode. Changing it persists this date's optimizer_mode override, preserving other override fields and user defaults. Restore it on reload; allow reset to the inherited engine. Briefly explain Normal's minute-level starts and ADHD friendly's quarter-hour starts for longer tasks, without claiming medical benefits. Update preference capabilities and persisted freshness after selection. Disable selection while saving/generating; a failed save must not run a different engine silently.

Actions: Import CSV, Export CSV, Make Schedule, Reset Day. Generation returns already_current as a no-op, adds new work around locked committed placements, and exposes explicit regeneration when existing work became incompatible. Never auto-generate merely because the dropdown changed. Display real unscheduled/mandatory-failure messages; do not erase the prior valid schedule on failure or no-capacity/no-new-work outcomes. Reset shows the backend preview and requires confirmation; it removes the described date data/overrides, clears the form after success and retains history/defaults.

CSV uses canonical v2 upload/download endpoints with preview, line errors, version/ownership conflicts and all-or-nothing import. Do not default to the legacy half-hour export. Preserve IDs, recurrence and relationships; invalidate all affected date/project/freshness queries after mutations, including cascades. Preserve existing execution/history functionality and identifiers; no need to rebuild analytics here.

Acceptance tests: today's date in different zones; 10:13 start and 13-minute duration; 12 AM/PM/next midnight; invalid block leaves storage unchanged; tag Enter/removal; edits preserve hidden fields; both engine mappings, saved restoration and default/date isolation; first generation, unchanged no-op and incremental preservation of IDs/times; stale after task/preference/engine change and current after explicit regeneration; reset cancellation/confirmation; canonical CSV roundtrip; failures preserve form/schedule. Cover real persistence through API integration and component tests. Run frontend test/lint/typecheck/build, relevant Python suites and full Python checks if backend logic changes. Document actual behavior.
```

### Prompt 4 — Week Schedule and real calendar Month

```text
Implement Week and Month after the complete Day page, reusing its forms, task cards, category/time components and API hooks.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md and relevant frontend/date/allocation/service/tests. Inspect actual prior-prompt work; do not assume requested features exist. Use the current repo as truth, preserve working behavior, implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactors, and report files, architecture choices, tests, commands/results and limitations.

Week defaults to the current calendar week in the planning timezone, with an explicit documented Monday start matching current desktop behavior. Render seven real dates, weekday/date headers, chronological scheduled work with a time axis, usable vertical/horizontal scrolling, and consistent fixed/flexible category colors. Unscheduled or only allocated tasks must be visually distinct and must not imply invented start times. Muted past-day styling retains readable contrast and text/status, and never deletes data.

A single click/keyboard action selects a date. Provide an explicit Open Day action; optional double click is only a shortcut. Day entered from Week offers Back to Week and restores week, selected date and useful scroll context via router state/URL. Reuse the task form to create work for the selected date and refresh affected views. No separate Week Make Schedule is required: route to the actual Day workflow. Reset Week uses a server preview/confirmation with real range semantics, cascades and atomicity; never seven uncoordinated deletes.

Month is the actual calendar month with correct leading/trailing weekday alignment and 28/29/30/31 days. Default to current month, show its name prominently, and offer all months of the current year in an accessible dropdown. Handle a year change without stale options. Out-of-month cells are clearly distinguished. Preserve past data and optionally mute past month choices without disabling access.

Each day is selectable/openable accessibly; preserve Month return context. Display real persisted dates. Within generated dates, summaries follow actual chronological intervals; before generation, show input/creation order or explicit allocation status without fake chronology. Truncate long summaries with a discoverable overflow count. Selecting a task must not accidentally trigger destructive actions or open the wrong day.

Use bounded range API queries, including all pages where required, and account/backend/range-aware cache keys. Edits, sync, CSV, reset and preference changes invalidate affected calendar/freshness data. Do not fetch one unbounded global collection or silently drop records after the first pagination page. Handle loading, empty, unavailable and stale states while navigating quickly; late responses cannot repaint the wrong month/account.

Acceptance tests include February with 28 days, leap February with 29, 30/31-day months, weekday alignment, December/January week transitions, current-year month options, timezone-local today, past-state styling, chronological ordering, select/Open Day/Back context, selected-date task creation, scoped reset and cancellation, fixed-category colors, pagination and network errors. Resize across phone/tablet/desktop widths and keyboard-navigate the date controls. Run frontend tests/lint/typecheck/build and relevant backend range tests. Update README/help only where workflow changed.
```

### Prompt 5 — Allocation Planning and real Projects

```text
Implement Allocation Planning and Project Schedule after Week/Month. These are real application views, not permanent placeholders.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md, current allocation/project/domain/repository/API/frontend/tests. Inspect actual output of all earlier prompts and treat current code as truth. Preserve working functionality, implement only this scope, add/update tests, run checks, fix introduced regressions, update docs, avoid unrelated refactors, and report files, decisions, tests, commands/results and limitations.

Allocation is deciding which date a task belongs to, not producing intraday intervals. Use the real backend preview/allocation service. Provide a bounded week/month range control and an explicit Allocate/Recalculate action; show assigned task cards by date, remaining capacity, unallocated work with service reason/explanation, required/deadline/project information and status. Do not label greedy allocation failure as proven impossible unless the response says so.

Show when a preview was computed from stale inputs and refresh deliberately. Persisted tasks/preferences/blocks are authoritative; do not keep the only meaningful plan in React memory. Reopening must load/recompute from those inputs. Preserve allocation range/fingerprint when opening a selected Day; generate only that date after backend revalidation. A stale preview cannot overwrite a changed schedule. Do not silently turn an allocation into task.required_date or generate the whole month while opening one day.

Build project list/create/detail/edit/delete through existing CRUD. Fields currently supported are name and description; there is no archive field, so do not display a nonfunctional Archive action. Show loading/empty/error states and recoverable conflicts. Respect version preconditions. A project with live tasks cannot be deleted; explain the backend refusal and let the user reassign/remove those references explicitly, without implicit cascading deletion.

Provide task-project assignment and clearing in the reusable task form and useful project filters in Day/Week/Month/allocation where appropriate. Keep project_id as an ID, preserve ownership and reject stale/cross-account references. Project Schedule shows its tasks and actual scheduled/unscheduled work with links to their dates; names alone are not identities. Filtering is a view operation: it must not remove other projects' tasks from the scheduler's hard-constraint/dependency context or reallocate the account unintentionally.

Unknown/deleted project references must have honest error/empty states. Do not create a new frontend-only project store or silently drop dependency context when fetching project tasks. Preserve recurrence data without expanding rules. All mutations persist through refresh, restart and the existing sync workflow.

Acceptance tests: create/rename/description/delete-empty project; refuse delete-in-use; assign/reassign/clear project; account ownership and stale update rejection; scheduled/unscheduled projection and links; mixed-project dependencies remain valid; allocation capacity/deadline/dependency reasons; selected-day generation honors the checked allocation; changes invalidate a preview; reload reproduces meaningful allocation from persisted data. Test pagination and empty/error states. Run frontend tests/lint/typecheck/build, focused project/allocation/backend tests and full Python checks if services changed. Document the difference between allocation and Day scheduling and the absence of project archive/recurrence expansion.
```

### Prompt 6 — Settings, active-engine preferences, Account and About

```text
Finish Settings, Account and About after Allocation/Projects. Reuse the Day Preferences controls and account/session architecture; do not create parallel settings state.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md, actual prior-prompt changes, app/planning/preferences.py, app/reward.py, config/task_preference.yaml, and related APIs/frontend/tests. The current repository is authoritative. Preserve working features; implement only this scope; add/update tests; run checks; fix regressions; update docs; avoid unrelated refactoring; report files, architecture choices, tests, commands/results and limitations.

Settings includes Language (English only, extensible options model), Appearance (Light/Dark, durable choice), and default scheduling preferences. Editing defaults uses the user layer; Day Preferences uses the date layer. Show effective inherited values and explicit overrides. Reset one override by removing its contribution, not by writing zero/default over the inherited value. For category dictionaries, preserve the difference between absent, a value, and explicit null; show any clear-versus-inherit action distinctly. Reset-all-date must retain user defaults.

Default engine choices use the same labels everywhere: Normal=precise_greedy, ADHD friendly=adhd_friendly. Normal is the fallback, not a forced overwrite of saved preferences. The Day dropdown remains adjacent to Make Schedule and edits only its date. Changing defaults updates inheriting dates and freshness but does not erase explicit date overrides or regenerate schedules automatically.

Audit the scorer/canonical adapter again and implement a documented control capability map. Expose meaningful day window, category multipliers/windows, importance/time/tag/fragmentation weights, preferred-time distance, tag proximity/relations, and gap threshold controls with human labels and validated bounds. Expose short_gap_bonus_weight/max_minutes/cap only for ADHD friendly, explaining zero disables the bonus. Do not expose unused weight_category_bonus, legacy exact-task-name YAML dictionaries, simulated-annealing parameters or hard-constraint bypasses. Do not change scoring merely to make a control useful. Tag scoring currently uses the first stored tag; preserve the full tag array. Gap settings are soft preferences, not enforced rest breaks. Avoid raw YAML/config names in normal UX.

Appearance and English-language selection use durable UI preference storage appropriate to the established profile, separate from optimizer preferences. If browser-local appearance is used, identify it as device-specific, and do not claim it syncs. Timezone must be explicit and consistent with the service: validate IANA zone, display it, and if editable persist through the application setting contract and invalidate freshness. Never silently replace saved timezone with browser detection on each page load.

Account displays actual /me data: display name (honest fallback if absent), masked email showing a small identifying part, sign-in/session status and plan. The backend currently has no billing/plan field; centralize a Normal fallback that can use a future server value, and do not fabricate subscriptions. Include the exact intentional empty state: “Performance analytics will appear here in a later milestone.” Existing execution/productivity data is not deleted or reset by visiting this page.

About briefly states that this is a personal scheduling/productivity project created by Ramtin Rezaei to experiment with intelligent scheduling and productivity tools. Avoid promotional claims.

Acceptance tests cover default/date precedence, individual and full reset, absent/value/null category semantics, engine capability visibility, disabled ADHD bonus, persistence/reload/sync where supported, date dropdown consistency, schedule staleness, invalid bounds and save conflicts, light/dark persistence, English-only control, masked real account information and Normal plan fallback. Run frontend tests/lint/typecheck/build and relevant preference/backend/Python checks. Update user documentation to match actual active controls and limitations.
```

### Prompt 7 — Whole-application verification and production hardening

```text
Complete Milestone 4 by verifying the entire application built by Prompts 0A–0C and 1–6. Do not trust prior summaries or passing unit tests as proof that the workflow is connected. Fix only issues necessary for the requested Milestone 4 application to work correctly; no new roadmap features.

Read AGENTS.md, CLAUDE.md, README.md, docs/web-api.md, backend/sync/deployment docs and relevant current implementation/tests. Inspect all prior work before editing; code is authoritative. Preserve working behavior, keep fixes focused, add meaningful regression tests, run verification, fix introduced regressions and update docs. Report files changed, architectural decisions, tests added, exact commands/results, any skips/failures, and remaining limitations. Do not weaken tests or silently ignore failures.

Verify both supported profiles: (1) hosted browser -> FastAPI -> PostgreSQL with real scheduling and no desktop process; (2) browser -> local FastAPI -> durable SQLite with the existing sync client talking to a backend. A missing local service must not make the hosted scheduler a mock. Disconnected hosted writes must fail honestly; local offline writes/generation must persist and later sync. Do not claim browser-only offline scheduling has been implemented.

Create Playwright E2E coverage with isolated test accounts/databases and real APIs. At least one complete flow must: launch services/frontend; register/login; create project plus fixed/flexible minute-precise tasks; configure defaults/date preferences; select Normal beside Make Schedule; generate; press again and prove no duplicate/version-only writes; add higher-priority work and prove retained placement IDs/times; inspect Week/Month; edit an input and observe persisted stale state; select ADHD friendly and explicitly regenerate; reload/restart and verify persistence; associate local data only after confirmation; sync to a second client; provoke and resolve a real conflict; export/import canonical CSV without identity loss; logout without leaking cached data. Include execution-history preservation and distinct recurring-date placements without inventing recurrence expansion.

Audit CSV cases: duplicate IDs, invalid rows/types/dates/timezones, owner mismatch, foreign references, stale versions, tombstones, unsupported version, invalid derived columns, multiline quoting and complete rollback. Canonical format v2 remains default; do not describe it as a full application backup. Reject arbitrary filesystem paths and enforce practical upload limits. Validate generation/reset/import concurrency and failure atomicity across persistence and change capture/logs.

Verify preferences and freshness after application restart, login/logout, two-device sync, task/block edits, default/date changes, engine selection and allocation input changes. Ensure UI displays trustworthy unallocated/unscheduled reasons, not a fabricated explanation from a saved count. Test unsupported DST/overnight time inputs with helpful errors and no silent rounding.

Check all routes/features: collapsed sidebar; today; Day task CRUD and category colors; exact times/tags; fixed-block rejection before persistence; free-time display; engine dropdown/defaults; Day Preferences; confirmed Day/Week reset; Week selection/Open Day/Back; true 28/29/30/31-day Month; Project CRUD/assignment/filtering; Allocation view/reasons; account/association/sync/conflict controls; Settings language/theme; Account data/plan fallback; About attribution. Remove intermediate placeholders except the explicitly allowed Performance empty state and unsupported-feature explanations.

Exercise keyboard-only operation, dialog focus trapping/return, labels, screen-reader names, visible focus, non-color status and readable contrast. Check light/dark at 390/768/1280/1920px, browser zoom, long labels, many tasks/tags, scrolling, slow APIs and rapid navigation. Resizing must be stable; important actions cannot depend on double click. Use accessible dialogs/toasts rather than ordinary browser alert flows. Verify actual screenshots manually as well as automated checks.

Harden deployment without deploying: production asset build served by the configured server/proxy, working SPA deep links, correct /api errors, explicit allowed origins/session/CSRF controls, cookie settings appropriate to HTTPS, secrets excluded from assets, migrations before readiness, and clear logging without tokens/stack traces in UX. Preserve desktop bearer sync. Resolve tested dependency compatibility/deprecation issues; add login throttling or an explicit deployable reverse-proxy limit for publicly exposed auth rather than calling an unrestricted dev server production-ready.

Run python -m pytest, python -m ruff check ., compilation, frontend clean install/tests/lint/typecheck/build, and the full Playwright suite. Verify actual process startup and health/readiness, not only imports. Run PostgreSQL-specific plus backend/sync suites on a disposable PostgreSQL database using the existing CI/compose pattern, and add application-operation coverage there. If an environment prerequisite is unavailable, record exactly what is unverified and provide the command/CI job; do not label that check passed. Ordinary automated tests must still work without external production services.

Update README and deployment docs with supported Python/Node versions, dependency installation, local/backend/frontend commands, both runtime profiles, environment variables, authentication and expiry, explicit local-data association, sync/conflicts, tests, production build/assets, migrations and Render expectations. Remove contradictory claims that the new app is desktop-only, always half-hour, automatically loads sample data, or has no project CRUD. Verify commands against the actual repository. Do not contact/deploy to a real hosting account in this prompt.

Final acceptance: a user can complete the documented workflow through the web UI, persist/reopen their data, use each engine, generate without duplication, retain committed work on incremental additions, manage projects/preferences, use real calendars and the established sync/conflict workflow. Provide a concise pass/fail matrix and material remaining limitations, including no recurrence expansion and no project archive if still unsupported. Do not mark Milestone 4 complete while a required workflow is represented by a mock or an unresolved integration blocker.
```
