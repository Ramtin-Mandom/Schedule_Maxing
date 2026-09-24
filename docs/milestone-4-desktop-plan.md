# Milestone 4 — Desktop UI rebuild with optional future web components

## A. Current scope and repository observations

The user's latest instruction supersedes the web-only wording in the pasted brief: **rebuild the desktop application now; retain the work from the previous 0A, 0B and 0C as reusable shared services and optional web components.** Do not implement a browser frontend in this sequence. The intended desktop remains a local Python/CustomTkinter application with SQLite persistence and optional cloud synchronization through FastAPI to PostgreSQL.

Claude is currently finishing the earlier 0A–0C. **Finish that work before starting the new Prompt 0 below. Do not rerun the old 0A–0C or continue with the old web UI prompts 1–7.** The old prompt files are left untouched so that this planning work does not modify instructions Claude may currently be using.

A read-only snapshot of the active working tree found new `app/planning/workflow.py`, `scope.py`, `fixed_block_rules.py`, `backend/planning_api.py`, `planning_repository.py`, `browser_sessions.py`, a browser-session migration, and changes to shared services/controllers/tests. The workflow already describes shared generation, incremental mode, no-op current schedules and fingerprint checking. These are observations of work in progress, not proof that all implementations and tests are complete. Prompt 0 must inspect the finished state, including whatever 0C ultimately creates.

The previous audit found a working canonical engine, local persistence, cloud record/auth/sync infrastructure, project CRUD, execution history, preferences, provenance and canonical CSV. It also found fixed-block write validation, reset, timezone, account scoping and incremental scheduling gaps. Some are now being repaired; do not duplicate or undo those repairs. The earlier result of 1,107 passing tests applies to the earlier audited state, **not** the currently changing worktree. This revision changes planning documents only; it does not certify the new code or modify Claude's application changes.

## B. Architecture to preserve

```text
Desktop widgets -> desktop controllers -> shared application/domain services
                                            |
                                            +-> SQLite repositories
                                            +-> allocator / scheduling engine
                                            +-> execution / preferences / provenance

Desktop account/sync controller -> existing SyncService -> HTTPS cloud API -> PostgreSQL

Optional future web entry point -> HTTP/session adapter -> shared application services
                                                        -> appropriate storage adapter
```

Keep shared scheduling/model/validation improvements in shared Python code. Keep HTTP routing, browser sessions, web hosting and SQLAlchemy/PostgreSQL adapter concerns in independently launched server/web components. Both clients can reuse domain logic without sharing UI state, mutable account globals, or an implicit running server. Isolation means clear dependency direction and explicit lifecycle, not duplicating the engine or reverting beneficial shared changes.

Desktop scheduling, editing, preferences, CSV and local execution must work without internet, a backend process, browser assets or cloud credentials. Cloud connection is optional; it uses the existing synchronization protocol rather than direct PostgreSQL credentials on the desktop. Future web services remain testable and launchable separately. An already-applied web migration is preserved; it is not deleted because the desktop does not need it.

Retain CustomTkinter/Tkinter and existing controller/repository patterns. Use reusable Python widgets, geometry managers, a bounded canvas for timelines, existing background-worker infrastructure and pytest/controller/widget tests. No React/Vite migration, embedded webview replacement, localhost dependency, or Node build prerequisite is needed for this desktop milestone.

## C. Execution order and coverage

Run the new prompts strictly sequentially after the old 0A–0C finish.

| Prompt | Scope | Main source-brief coverage |
| --- | --- | --- |
| 0 | Audit/isolate 0A–0C and verify desktop compatibility | Prerequisites, shared logic, persistence, regressions |
| 1 | Desktop shell, navigation, theme and reusable controls | Visual style, sidebar, layout, accessibility, resizing |
| 2 | Desktop account, explicit association, sync and conflicts | Account connection, ownership, online/offline, conflict UI |
| 3 | Reusable task form and task interactions | CRUD, minute precision, tags, fixed validation, field preservation |
| 4 | Day timeline, engine dropdown, preferences, generation, CSV/reset | Complete Day workflow, freshness and free time |
| 5 | Week and actual calendar Month | Calendar arithmetic, selection, navigation, scoped resets |
| 6 | Allocation Planning and Project Schedule | Allocation reasons/status, real project CRUD and assignment |
| 7 | Settings, account details and About | Defaults/overrides, active-engine controls, theme/language, attribution |
| 8 | Complete desktop workflow verification and UI polish | End-to-end tests, persistence/sync, compatibility and documentation |

Splitting task widgets from the Day workspace keeps the main screen prompt bounded and lets Week/Month reuse the same form. All requested product behavior is retained; browser-specific requirements are translated to native desktop equivalents. Existing Execute/Productivity functionality must remain accessible. Recurrence expansion, unsupported project archive and a browser UI remain outside scope.

## D. Full Claude Code prompts

### Prompt 0 — Audit completed 0A–0C and protect desktop compatibility

```text
The previous Milestone 4 prompts 0A, 0B and 0C introduced domain repairs and web/server components. This is the NEW Prompt 0, to run only after those tasks finish. Audit their actual changes, isolate optional web concerns, and ensure the existing desktop app still works before its UI rebuild. Do not implement the new UI or expand the web app in this prompt.

Read AGENTS.md, CLAUDE.md, README.md, docs/milestone-4-desktop-plan.md, and relevant current code/tests/config/docs. Inspect git status, committed and uncommitted changes, the old prompts, and their actual implementation. Do not assume completion from an earlier request or report. Preserve other work; do not reset the branch or discard changes. Use the repository as truth, make only necessary compatibility repairs, add meaningful tests, run checks, fix regressions, update docs and report files, decisions, tests, commands/results and remaining limitations. Finish fully before Prompt 1.

Inspect app/app.py, app/ui/app_services.py, planning_controller.py, schedule_page_controller.py, background.py; app/planning/application.py, repository.py, workflow.py, scope.py, fixed_block_rules.py, allocation.py, preferences.py, provenance.py; execution/productivity/sync modules; backend/app.py, planning_api.py, planning_repository.py, browser_sessions.py, routes/migrations/settings; all local web components created by 0C; dependencies and relevant tests. Discover actual names rather than relying on this list.

Classify the changes into (a) shared domain/application improvements, (b) desktop controllers/storage/sync, (c) optional HTTP/browser/server adapters. Retain shared correctness improvements and one engine. Desktop widgets call controllers/services directly, never localhost HTTP routes. Web routes can call shared services; shared services must not import backend routes, FastAPI dependencies, browser sessions or Tk widgets. Current desktop controller result helpers may import Tk infrastructure; do not reuse those UI modules as a server/domain dependency. Extract a small neutral helper only where necessary; avoid an architectural rewrite or file moves for appearance alone.

Ensure python -m app.app starts with the cloud URL unset, no DATABASE_URL/JWT_SECRET, no running HTTP/PostgreSQL server, no browser assets, no Node tooling and no internet. It must not start a web server, reserve a listening port, migrate a cloud DB or initialize browser auth on desktop import/startup. Sync remains inert until explicitly configured/authenticated. Verify this in an isolated desktop runtime without web-only packages, adjusting dependency grouping only if necessary. Preserve the full development install/test path and optional web/backend dependencies, factories and launch commands.

Verify data compatibility using copies/temporary fixtures, never the user's live database: old SQLite migration, reopen, IDs, timestamps, owner fields, local versus server versions, recurrence, execution links/history, canonical CSV, defaults/date overrides and schedule freshness. Preserve any already-applied server migrations, including browser-session changes. Never remint IDs or delete a migration to isolate the desktop. Existing web endpoints and bearer-token sync must continue working through their own explicit entry points.

Check the newly shared workflow through existing desktop callers: first generation, unchanged no-op, incremental additions, explicit full regeneration, fixed constraints, timezone deadlines, dependency ordering, history protection, stale-input/placement races and atomic saves. Keep legacy full-generation and Greedy Optimizer v1 regression behavior. Reuse completed fixes rather than reimplementing them. Provide controller operations needed by UI prompts if the only current entry point is an HTTP route; move shared operation logic below the route instead of sending the desktop through a web server.

Audit account/backend scope for desktop reads, generation, preferences, executions, reset and export. Account switching must not mix records or late background callbacks. Preserve an explicit ownerless local workspace and require confirmation before association. Verify cloud failure never prevents local CRUD/scheduling, and workers never call Tk directly. Shutdown must finish/cancel work safely before closing SQLite; no writes through a closed connection. Explain whether simultaneous desktop/local-web use is supported; test safe locking or explicit exclusion instead of silently allowing unsafe concurrent ownership/session mutation.

Add import/startup isolation tests and functional desktop regressions. Run full pytest, Ruff and source compilation, focused backend/sync tests, actual desktop launch/widget smoke test, and CLI smoke tests with temporary data. Exercise the optional backend/local web factories separately; run real PostgreSQL tests if available and report skips honestly. The prior 1,107-test result is historical, not evidence for current changes. Fix required failures before advancing.

Acceptance: desktop can create/edit/delete, generate, execute, export/import and reopen local data with web components absent/stopped; cloud sync works when configured; optional web components remain separate and functional; no duplicate domain model/engine is introduced. Write docs/desktop-web-boundaries.md with a module/dependency map, independent launch commands, supported concurrency, compatibility results and the exact controller contracts the next prompts should use. Do not declare success based on imports alone. Do not begin the new UI until the working desktop baseline is verified.
```

### Prompt 1 — Desktop shell, design system and stable layout

```text
Rebuild the desktop presentation foundation after the NEW Prompt 0. Keep Python/CustomTkinter; no browser UI, webview replacement, local HTTP requirement or Node build. Optional web components remain separate for later use.

Read AGENTS.md, CLAUDE.md, README.md, docs/milestone-4-desktop-plan.md, docs/desktop-web-boundaries.md, app/app.py, app/ui/theme.py/background.py/app_services.py and current tests. Inspect actual prior implementation; never assume a requested feature exists. Preserve working functions and data, implement only this scope, add/update tests, run checks, fix regressions, update usage docs, avoid unrelated refactors and report files, architecture choices, tests, exact commands/results and limitations.

Extend established UI/controller conventions. Break large presentation code into cohesive reusable widgets/pages only where this rebuild needs it. Maintain a clear shell/page/controller boundary; widgets never implement scheduling, SQL or synchronization. Keep existing Execute and Productivity functionality reachable throughout the rebuild. Intermediate new-page placeholders must not remove working old workflows; final placeholders are only those explicitly allowed by the brief.

Create a left sidebar that starts collapsed and opens through a keyboard-focusable hamburger button. Navigation: Day Schedule, Week Schedule, Month Schedule, Project Schedule, Settings, Account, About; provide access to Allocation Planning and retain existing execution/productivity access. Default to Day Schedule. Preserve selected date/week/month when moving between pages. Use grid/pack correctly per parent, row/column weights, sensible minimums and scroll regions. Stack panels or collapse navigation at small window sizes rather than clipping fields.

Fix window move/resize shaking. Avoid mutually recursive Configure/geometry updates, rebuilding pages on every pixel change and unbounded redraw loops. Coalesce canvas redraws through after/after_idle, cancel obsolete callbacks, and keep expensive work off the Tk thread. Animation must have bounded callbacks and stop when its widget is destroyed. Do not use a global minimum size as a substitute for usable resizing.

Create reusable styled buttons, labelled entries/selects, cards, contextual menus, dialogs/drawers, notices, confirmation and loading/empty/error states. Use rounded controls/blocks, soft borders, modest shadows where supported, readable fonts, generous consistent spacing, muted/pastel category colors and genuine light/dark palettes. Centralize category colors for flexible tasks and actual fixed-block categories, including unknown imported categories without remapping their data. Avoid excessive gradients, saturation and clutter.

Provide keyboard traversal, visible focus, associated visible labels, Escape/Enter behavior, focus return after dialogs and status text beyond color. For canvas tasks provide a keyboard-operable task list/actions, not mouse-only objects. Respect DPI/font scaling. Persist appearance through a small UI settings boundary; do not mutate reward globals or store secrets with appearance. UI updates from workers must run on the Tk thread and ignore stale/destroyed/account-switched targets.

Acceptance: the real app launches on Day with collapsed sidebar; navigation and retained old functions work; light/dark persist; repeated move/resize/minimize/restore and DPI scaling produce stable readable layouts; narrow windows retain reachable controls/scrolling; keyboard dialogs and focus work. Add headless presenter/theme tests plus real widget tests where a display exists. Run python -m pytest, python -m ruff check ., compilation and native visual smoke checks. Report any display-dependent tests not run, without treating them as passed. Document desktop launch and layout behavior.
```

### Prompt 2 — Desktop accounts, local-data association, sync and conflicts

```text
Implement desktop account/connection and synchronization controls after Prompt 1, using the existing SyncService and shared services from Prompt 0. Do not call the optional local web gateway or put a database password in the desktop.

Read AGENTS.md, CLAUDE.md, README.md, the current desktop plan/boundary docs, app/sync modules, app/ui/app_services.py and account/sync tests. Inspect actual earlier changes and use current code as truth. Preserve working offline use and optional web APIs. Implement only this scope, add/update tests, run checks, fix regressions, update documentation, avoid unrelated refactoring and report files, decisions, tests, commands/results and limitations.

Build native registration, login, logout, active-account display and backend connection configuration/status. Use existing API/transport contracts for account operations; reuse public service operations completed by 0C, extracting transport-independent functionality if it was placed only in routes. Do not reach into private HTTP handler internals from widgets. Validate fields, disable duplicate submissions, show recoverable validation/auth/network errors, and run network calls in background workers. Passwords are not persisted; retain the established memory-only access token behavior unless an explicitly approved credential design already exists. Clearing the local session is logout; do not claim server token revocation if unsupported.

Ordinary local scheduling must work without login or internet. Make account-owned and ownerless workspace scope explicit using Prompt 0's rules. Switching account/backend invalidates pending view loads and rebinds controllers safely; late results cannot show or mutate another account's data. Never automatically upload or claim old local records during login. Allow account-owned new work according to the service without accidentally associating the old ownerless workspace.

After signing in, show the real unassociated-data preview with counts, ownership conflicts and scope explanation. Confirm calls the actual association service; cancel/dismiss changes no ownership, versions or sync state. Revalidate a stale preview. Preserve IDs and history, and never claim another account's records.

Add compact connection/sync indicators and Sync now. Show online/offline, pending changes/count when available, in progress, last successful sync, open conflicts and actionable failure/auth-required text. Distinguish unconfigured, signed out and unreachable. Status derives from the durable service/outbox, not a widget counter. Ensure last-success status survives restart where promised. Reuse backoff/idempotency/change capture; no second sync algorithm or overlapping background loop. Refresh relevant views/freshness after successful sync or remote changes.

Provide conflict list/details comparing labelled local and remote values, timestamps/versions where available and deletion/tombstone state. Offer only service-supported resolutions, typically Keep local and Accept remote; do not invent Merge. Explain unavailable keep-local for remote deletion/scope collision or divergent history. Apply decisions through the service with loading/error feedback; never silently last-write-wins.

Acceptance: real register/login/profile/logout, expiry recovery, offline create/reopen, explicit association cancel/confirm, account/backend isolation including in-flight work, two-device sync, lost-response retry, durable pending status, deletion and allowed conflict decisions. Test with temporary SQLite and the existing in-process backend/failure transports, plus desktop widget flows. Close during network work without freezes, callbacks to destroyed widgets or a prematurely closed database. Run full Python tests/lint/compile and desktop smoke checks; document connection, sign-in, association and recovery steps.
```

### Prompt 3 — Reusable minute-precise task form and task actions

```text
Implement reusable desktop task entry/editing and task actions after Prompt 2. Day, Week, Month and Projects must reuse this implementation. Keep persistence and validation in the existing shared services.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs, canonical models, scope/validation services, current schedule_page_controller/form code and tests. Inspect every prior prompt's implementation; current code is authoritative. Preserve working functionality, implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactors and report files, decisions, tests, commands/results and limitations.

Derive fields from the canonical model, not the old desktop form. Flexible tasks support name, category, estimated minute duration, priority 1–10, required flag/date, preferred dates/window, deadline, dependencies and project. Group advanced optional fields without hiding needed functionality. Fixed blocks use label/category/date/start/end and are not fake flexible tasks. Never ask users for UUID, user_id, timestamps or versions. Discrete fields use selects; dependency/project selections retain IDs and show meaningful labels. Preserve unknown imported categories and duplicate task names correctly.

Replace the half-hour restriction with an accessible native time control supporting direct typing and picker/spin/scroll input at one-minute precision. Display h:mm AM/PM, allow one-minute durations, and correctly handle noon, midnight and an interval ending at next midnight. Users must not type minutes-from-midnight. Convert through existing IANA-zone/aware-time helpers. Unsupported overnight, ambiguous/nonexistent times or offset-transition windows get actionable validation without silent rounding. Test 10:13 and a 13-minute duration.

Tags use the actual ordered string array. Enter adds a chip without submitting the task; chips can be removed and wrap/scroll. Do not impose the old mandatory single-tag constraint. Preserve tag ordering and any current first-tag scoring semantics without changing the engine.

Save through controllers/services with version preconditions and current owner scope. Fixed-block save validates start/end, day window and overlap before persistence, excluding itself during edit. Keep form contents on errors and identify the conflict. Failed saves do not create visible phantom records. Build edits from existing models so recurrence, extra tags, dependencies, project links and unknown presentation fields are not dropped by partial forms. Preserve recurrence data; no Repeat control unless full expansion exists.

Task cards/rows offer edit and remove via a clean contextual/action menu and keyboard access. Confirm substantial destructive operations; show real dependency/ownership/conflict refusal rather than hiding it. Selection is ID-based. Fixed/flexible type conversion follows service support; do not silently delete/recreate identities. Refresh committed data after success and preserve execution history.

Acceptance tests cover valid/invalid required fields, minute parsing/picker equivalence, AM/PM/midnight, tag Enter/removal, fixed-window/overlap errors with no write, duplicate names, stale versions, owner isolation, project/dependency references, edit preservation of recurrence/hidden fields and delete refusal. Use headless form/controller tests plus real widgets. Run pytest/Ruff/compile and a native create/edit/delete/reopen smoke test against a temporary DB. Document field semantics and time limitations.
```

### Prompt 4 — Day workspace, engine dropdown, preferences and schedule actions

```text
Build the complete desktop Day Schedule using Prompt 3's task widgets and the shared workflow verified by Prompt 0. No HTTP round trip is needed for local scheduling.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs and current UI/planning/workflow/preferences/provenance/CSV/tests. Inspect actual previous work; do not assume earlier requests were fulfilled. Preserve working features and optional web boundaries. Implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactoring and report files, decisions, tests, commands/results and limitations.

Open today's real date in the configured planning timezone and load persisted state. Provide date navigation and retain Week/Month return context. Upper workspace: a horizontal schedule timeline with hour markers, minute-proportional actual intervals, readable h:mm AM/PM details and accessible task actions. Fixed blocks appear immediately and use their stored category color; flexible placements appear after generation. Below, show available/unplaced tasks in a scroll/wrap area, with genuine unscheduled explanations when known. Derive Free time gaps inside the effective scheduling window only for display; never save fake tasks. Lower workspace: reusable Add Task panel and clearly separated actions.

Place a labelled Engine dropdown immediately beside Make Schedule:
  Normal -> precise_greedy
  ADHD friendly -> adhd_friendly
These are the currently supported engines; derive capability validation from the current enum/service, with human labels centralized for future modes. Normal is the fallback when no saved preference applies; do not overwrite an existing preference on startup. Show the date's effective mode. Selection saves only this date's optimizer_mode override while retaining other fields and user defaults; offer reset to inherited mode. Persist/reload it and update freshness. Disable during save/generation and never silently generate with a mode whose save failed. Explain the practical difference briefly: Normal allows minute starts; ADHD friendly uses quarter-hour starts for tasks over 30 minutes, with short tasks still minute-precise and durations unchanged. No medical-effect claims.

Day Preferences is a native dialog/drawer, not a YAML editor. Load effective/inherited values and sparse date overrides. Show which values are inherited; save/reset one field or the whole date correctly, including absent/value/null dictionary semantics. Share these controls with later default Settings. Only show meaningful active-engine controls; do not expose inactive category bonus or legacy/annealing settings. Spacing is a soft preference, not a guaranteed break. Persist through services, not module globals.

Actions: Import CSV, Export CSV, Make Schedule, Reset Day. First generation uses current date/tasks/fixed blocks/preferences/engine and persists atomically. Repeated current generation returns already_current without redundant records/versions. Added work uses the explicit incremental operation to retain valid committed IDs/times. If edits/engine changes invalidate retained work, explain and offer explicit regeneration respecting execution/history/occurrence rules. Do not regenerate just because an engine changed. A failure/no-work/no-capacity outcome preserves the prior schedule; display mandatory and optional failure reasons accurately. Use background workers and guard against date/account changes while running.

Freshness comes from persisted provenance and shared classification after reload, restart, sync, task/preference/allocation/engine changes. Show Current/Out of date and reasons where available. Do not invent stored unscheduled explanations from counts. Reset previews actual scope/cascades and requires confirmation; successful reset clears the form, appropriate date planning records, date overrides and generation state while keeping inherited defaults/history. It must not zero all weights or delete unrelated/undated tasks.

Use native file dialogs with canonical identity-preserving CSV v2 parsing/export. Preview errors/updates and apply atomically with version/ownership checks; never fall back silently to legacy half-hour format. Handle duplicate IDs, bad rows, dates/times, references and unsupported version. Preserve recurrence/metadata and refresh every affected view.

Acceptance: full persisted Day flow including exact-minute layout, true fixed category colors, free gaps, both engine mappings/default/date isolation, preferences inheritance, unchanged no-op, incremental stability, stale/regenerate/reopen, failure rollback, reset cancel/confirm and CSV roundtrip. Add controller/widget integration tests; run full pytest/Ruff/compile and native UI smoke tests. Preserve Execute/Productivity access and existing execution records.
```

### Prompt 5 — Desktop Week and real calendar Month

```text
Implement Week and Month using the existing desktop shell, shared task form and completed Day workflow. Do not build browser routes or duplicate task/constraint logic.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs and current date helpers, range services, UI/controllers/tests. Inspect actual previous work; use the repository as truth. Preserve working functionality, implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactors and report files, decisions, tests, commands/results and limitations.

Week defaults to the current calendar week in the planning timezone, with documented Monday start matching existing behavior. Show seven real dates, weekday/date headers, a time axis and scrollable schedule columns. Before generation show task summaries in input/creation order or labelled allocation state, not invented times. After generation use actual chronological intervals. Consistent task/fixed-category colors apply throughout. Past days stay visible and muted with readable contrast; never remove historical data.

Single click or keyboard selects a day. Provide an explicit Open Day action; double click can be a shortcut only. Day entered from Week offers Back to Week and restores selected week/day/context. Reuse the lower task-entry workspace for the selected date. Scheduling is performed on Day, so do not introduce a separate Week Make Schedule requirement. Reset Week shows the service preview/confirmation and affects the described selected range atomically; retain history and expose/refuse out-of-range cascades appropriately.

Month must use actual year/month arithmetic and weekday alignment, not 30 days from an anchor. Default to current month, show its name prominently and offer the current year's months in a dropdown. Handle 28, leap-year 29, 30 and 31 days and rollover to a new year. Distinguish out-of-month cells. Past days/months can be muted without disabling viewing. Selecting/opening a day and returning to Month retain context, with keyboard-accessible controls. Show useful overflow counts for crowded cells and exact ordered details for generated work.

Load through bounded, scoped range controllers. Avoid per-pixel DB reads and page rebuild loops. Coalesce redraws on resize and discard stale date/account loads. Sync, imports, reset and edits refresh affected dates/freshness across open pages. Narrow windows use stable scroll regions/stacked panels rather than clipping controls.

Acceptance tests: all month lengths, leap February, weekday placement, week/year boundaries, timezone today, current-year selector, preserved past data, unscheduled versus chronological display, select/Open/Back context, selected-date task creation, reset cancellation/atomicity, fixed colors and asynchronous navigation races. Use headless calendar/presenter tests and native widgets; visually inspect common laptop/desktop sizes, small windows and DPI scaling. Run pytest/Ruff/compile and document the actual calendar workflow.
```

### Prompt 6 — Desktop Allocation Planning and Project Schedule

```text
Implement Allocation Planning and functional Projects after Week/Month. Reuse the local Python services directly; keep optional web adapters independent.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs, allocation/workflow/project models/services/controllers and tests. Inspect all actual prior changes; do not assume completion. Preserve working features, implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactors and report files, decisions, tests, commands/results and limitations.

Provide a discoverable Allocation Planning view with a bounded week/month range and explicit Allocate/Recalculate action. It shows which date each task is allocated to, remaining capacity, unallocated tasks, service reason codes translated into readable explanations, deadline/project/date information and allocation status. Allocation assigns dates only; it must not generate exact intraday placements or claim greedy failure proves impossibility unless the service explicitly establishes that.

Use actual persisted inputs and shared allocation methods. A derived preview can be recomputed on reopen; never imply a widget-only selection is a saved schedule. Mark stale previews after input changes. Opening an allocated date preserves its range/context and revalidates the fingerprint before Day generation; do not optimize every date on Open Day. Do not convert an allocation assignment to required_date unless a separate explicit domain operation supports it. No drag-and-drop allocation mutation is required by this prompt.

Use existing project CRUD to create, rename/edit name/description, list and view projects. Archive/delete are shown only if truly supported; at audit time archive was absent, while deletion was refused for projects with live task references. Show that refusal and let users reassign references deliberately; do not add implicit cascade deletion. Respect owner scope and version conflicts.

Enable project assignment/clearing in the shared task form and useful filters in calendar/allocation views. Project Schedule shows actual project tasks and scheduled/unscheduled work with links to dates. Selection/filtering uses IDs. Filtering a view must not remove other projects' fixed blocks/dependencies from scheduling input or change scheduling scope unexpectedly. Preserve recurrence fields and occurrence identity without implementing expansion. Retain unknown/deleted-reference error states instead of silently dropping data.

Acceptance tests: project create/edit/delete-empty/delete-in-use; assign/reassign/clear; stale writes and ownership; project schedule projections; mixed-project dependencies; allocation capacity/deadline/dependency reasons; preview invalidation; selected-day generation from a checked preview; reopen and sync persistence. Include errors, empty views, long task lists and keyboard navigation. Run Python tests/lint/compile plus native workflow smoke tests. Document allocation versus detailed scheduling and unsupported archive/recurrence functionality.
```

### Prompt 7 — Desktop Settings, account details and About

```text
Complete desktop Settings, Account details and About after Prompt 6. Reuse Day Preferences and account/sync UI; keep shared service behavior available to future web clients without a web dependency in desktop widgets.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs, current preferences/reward adapters, UI settings/theme/account code and tests. Inspect actual prior work, use the repository as truth and preserve functioning features. Implement only this scope, add/update tests, run checks, fix regressions, update docs, avoid unrelated refactors and report files, decisions, tests, commands/results and limitations.

Settings includes Language (English only, designed for future options), Appearance (Light/Dark, persisted), default scheduling preferences and default engine. Use the same human labels as Day: Normal=precise_greedy and ADHD friendly=adhd_friendly. Defaults apply to inheriting dates; never erase explicit date overrides. Changing preferences/engine updates persisted freshness but does not auto-generate. Appearance/language belong in UI preferences, not optimizer globals. Preserve explicit planning timezone; if editable, validate/persist it through the existing setting boundary and update date/freshness handling.

Use the shared preference editor and distinguish effective/inherited/overridden values. Reset one contribution by removing it, not setting zero. Keep category dictionary absent/value/null behavior correct and distinguish clear from inherit. Default edits persist to user defaults, Day edits to selected-date overrides. Show validation/save-conflict errors without losing edits.

Recheck current scoring semantics and maintain a tested capability map. Expose meaningful window, category multipliers/windows, priority/time/tag/fragmentation weights, preferred-time distance, tag proximity/relations and gap threshold controls with human labels. ADHD short-gap weight/max/cap only affect ADHD and zero weight disables it; hide or explain inactive controls. Do not expose unused weight_category_bonus, legacy exact-task-name YAML overrides, simulated annealing constants or hard-constraint bypasses. Do not alter engine/scoring to make a UI field meaningful. Current tag adapter uses the first tag; preserve all tags and avoid misleading claims. Gap threshold is a scoring preference rather than a mandatory break. No raw YAML editing.

Account details use the authenticated profile: actual name with honest fallback, email masked to leave a small identifying portion, session/account status and plan. At audit time there was no billing/plan field: use a centralized Normal fallback that can accept future server values, without inventing a subscription. Show “Performance analytics will appear here in a later milestone.” as the allowed empty section; keep the existing separate Productivity/Execute functionality and historical data accessible.

About briefly states this is a personal scheduling/productivity project created by Ramtin Rezaei to experiment with intelligent scheduling and productivity tools. No promotional or fabricated metrics.

Acceptance: default/date precedence, individual/all reset, category absent/null distinctions, engine-specific control relevance, both dropdown labels, reload/sync where supported, stale status, validation/conflicts, theme persistence, English-only control, real masked account data and Normal fallback. Test via headless controllers and native widgets; run pytest/Ruff/compile and visual light/dark checks. Document which settings sync versus remain device-specific and current engine/time limitations.
```

### Prompt 8 — Complete desktop workflow verification and final polish

```text
Verify and finish the entire desktop Milestone 4 from new Prompts 0–7. Inspect the actual application end to end, not just previous summaries. Preserve the optional web work from old 0A–0C as independently usable components, but do not implement a web frontend or deploy anything.

Read AGENTS.md, CLAUDE.md, README.md, desktop plan/boundary docs and relevant current code/tests/config. Do not assume a feature exists because a prompt requested it. Use code as truth, preserve working functionality, fix only required integration/UX issues, add meaningful regressions, run checks, update documentation and report files, decisions, tests, exact commands/results, skips/failures and limitations. Do not weaken tests or call a mocked workflow complete.

Run a realistic native desktop end-to-end flow against isolated data and an in-process/disposable cloud backend: launch offline; create fixed/flexible minute-precise tasks and a project; register/login; detect ownerless data; cancel association and prove nothing changes; confirm association; configure defaults/date preferences; choose Normal beside Make Schedule; generate; repeat and prove no duplication/redundant versions; add higher-priority work and retain prior IDs/times; inspect Week and true Month; edit a task, see stale state and explicitly regenerate; switch to ADHD friendly and verify engine behavior; close/reopen and restore data/preferences/freshness; sync to a second device; provoke/resolve a real conflict; CSV roundtrip; logout with no cross-account data exposure. Exercise execution Start/Pause/Resume/Complete and preserve snapshots/history. Recurring placements on different dates remain distinct without expansion.

Verify every requested screen and interaction: collapsed animated sidebar/default Day; stable resizing; timeline/hour labels; actual fixed categories; backlog/actions; typed/picker minute times and tags; pre-persistence fixed validation; Day Preferences inheritance; engine dropdown; free gaps; canonical CSV; confirmed Day/Week resets; Week Open Day/Back; 28/29/30/31-day Month and current-year selector; allocation reasons/status; Project CRUD/filter/assignment; account/backend/association/sync/conflicts; default Settings/English/light-dark; real masked account/Normal fallback/Performance empty state; About attribution. Remove intermediate placeholders except explicitly unsupported functions and the Performance empty state. Preserve existing Productivity/duration suggestion/Execute access.

Test persistence/freshness after restart, sign-in/out, sync, allocation/task/block/preference/engine changes. Invalid writes/imports/resets and stale-input races must roll back across records and sync capture. CSV covers duplicate IDs, owner/version conflicts, tombstones, invalid references/rows/times, unsupported versions, derived-field errors and quoting; preserve format v2 and do not claim it backs up all application state. Reset preserves defaults/history and honors confirmed scope/cascades. Unsupported DST/overnight inputs give useful errors with no silent rounding.

Stress native interaction: slow network, generation/sync in flight, repeated clicks, closing dialogs/window during work, account/date switches, long labels/many tasks/tags, keyboard-only navigation, focus return, non-color status, theme contrast, small windows, common laptop/desktop dimensions, DPI scaling, moving/resizing/minimize/restore. No Tk calls from workers, infinite Configure loops, stale callback writes or closing SQLite while a worker uses it. Use existing widget-test infrastructure plus headless controllers; document display availability and manual evidence. Native message dialogs are acceptable where appropriate; avoid raw stack traces and unhelpful modal error loops.

Reverify architecture boundaries: desktop starts and performs local workflows without internet, web-only packages, web servers/assets/browser sessions/cloud secrets/Node. Optional HTTP/server adapters still import/start/test separately, bearer sync remains compatible, applied server migrations remain valid, and shared domain code has no UI/HTTP dependency. Do not reset/delete working web components to get desktop tests green. Preserve/test the documented simultaneous-process policy.

Run full pytest, Ruff, source compilation, native desktop launch/widget tests, CLI smoke with temporary outputs, backend/sync suites and existing optional-web checks. Run real PostgreSQL migration/backend/sync tests on a disposable DB when available; record unavailable checks as unverified. Run any already-existing frontend tests only to check retained components if such a frontend was independently added, not to create one now. Normal tests must not require external production services. Update CI appropriately without concealing skipped UI tests.

Update README and desktop-web-boundaries.md with supported Python/install/run commands, local SQLite storage/backup, timezone and optional backend configuration, authentication/token lifetime, explicit association, sync/conflicts, engines/preferences, tests, safe shutdown, and separate optional server/web launch/deployment notes. Render applies to the cloud backend, not the native UI. Do not require npm or a web build to launch desktop. Check instructions against actual commands. Do not claim 0A–0C's earlier test results validate this final state.

Acceptance: a user completes the documented desktop workflow with durable real data, works offline, connects/syncs with the cloud when requested, and can use both engines/calendars/projects/preferences without regressions. The retained web component remains independent and ready for later expansion. Provide a pass/fail matrix, actual verification evidence and remaining limitations; do not declare completion with a broken required path or fake UI action.
```
