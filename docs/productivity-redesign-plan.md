# Productivity redesign: audit, shared contract, implementation prompts

Prepared 2026-10-04 against the local checkout, branch `SMV2-M6`, HEAD `ffb89f9`, including its current working-tree files. This document is a plan, not an implementation or a passing-test report. Existing staged documentation/test-infrastructure changes belong to the user and must be preserved. Recheck relevant files and migration heads before implementing; do not repeat a repository-wide audit.

Run the five prompts below sequentially. Each prompt references this document's shared contract and feature inventory, so keep this file available in subsequent sessions. No production database access or deployment is required.

## 1. Concise code audit

| Area | Existing implementation and finding |
|---|---|
| Desktop | `app/ui/productivity_page.py:ProductivityPage` builds filters, six summary tiles, two charts, insights, follow-through, history and data controls. It is not organized into General / Task-based / Time-based. `_render_summary` incorrectly computes skipped as terminal minus completed, which includes cancelled. |
| Analytics | `app/productivity/reporting.py:ProductivityService`, `stats.py:compute_segment_stats`, `segments.py`, `trends.py`, `insights.py` already calculate execution statistics. Preserve the execution-created-date basis. `segments.py:best_supported_time_bucket_by_category` chooses the largest completed-duration sample, not the highest completion rate. Do not relabel it as best performance. |
| Planned work | `app/planning/history.py:ScheduleHistory` and `collect_schedule_history`; `app/productivity/schedule_cohort.py:build_schedule_cohort_report` include never-started work, lineage, due outcomes, workload, start timing, estimation, rescheduling and data-quality signals. Reports currently cap ranges at 366 days and lineage traversal at 64 steps. All-time achievements require bounded multi-window aggregation, not removing those bounds. |
| Local lifecycle | `app/planning/application.py:PlanningService`, `workflow.py:reschedule_placement`, `recurrence.py`, and `repository.py` manage persistent plans. `app/execution/service.py:ExecutionService` supports timed completion, untimed outcomes, reopening and deletion. `lifecycle.py` is shared transition logic. Reopening clears the completion marker/metrics, so a permanently accumulating points counter would be wrong. |
| Points | `app/planning/models.py:Task.points` is a nonnegative user-defined productivity value, explicitly separate from `ScheduledTask.score`. `app/execution/models.py:TaskExecution.points` snapshots it when the execution is created. `day_summary.py:summarize_day` already totals completed points, but can fall back to current task points for old records. New historical achievements must not silently use that mutable fallback. |
| Day colours | `app/productivity/day_summary.py:classify_day` is the existing shared classifier: light green at 60% completed and dark green at 80% of scheduled work. Cancelled attempts count as uncompleted in this classifier. This differs intentionally from due-work completion, which excludes cancellations. |
| Identity | `Task` has UUID ownership, categories, tags and recurrence identities (`series_id`, `occurrence_slot`, `series_predecessor_id`). There is no explicit reusable task-type identity in the inspected model. An occurrence is not a type, and a category is not a type. |
| Historical gaps | `ScheduledTask` preserves category, interval, optimizer score/metadata, removal reason and successor; it lacks historical name, tags, task-type identity and task-points snapshots for work never started. `history_model.py:detail_text` uses current task names through `build_history_page`, so renamed unattempted work is not fully historical. Executions already snapshot name/category/tag/estimate/priority/points. |
| Storage | Local schema in `app/execution/db.py:MIGRATIONS` ends at 11; backend Alembic files end at `0012_scheduling_modes.py`. `backend/models.py`, `record_mapping.py`, `snapshots.py` implement normalized live and revision storage. Changes must cover revisions as well as live rows. |
| Sync/accounts | `app/sync/service.py:SyncService`, `engine.py`, `store.py`, `mapping.py`, `transport.py` provide durable outbox/retry, per-account cursors, conflict handling, explicit ownerless-data association and capability negotiation. `backend/sync.py`, `mutations.py`, `accounts.py`, `executions.py` provide authenticated server behavior. This infrastructure exists; do not rebuild authentication or sync. |
| Storage profiles | `app/ui/app_services.py` builds local/synchronized controllers; `direct_services.py` builds direct-server controllers. `productivity_controller.py:storage_copy` already explains device/account/server deletion semantics. Direct PostgreSQL intentionally has no local offline replica; preserve this private-development profile. The requested local-first signed-in behavior belongs to the existing HTTP-sync profile. |
| Verification | `docs/testing.md`, `tests/test_tiers.py`, `tests/conftest.py` define `python -m pytest -m dev`. Dev excludes real-window, system (sync/web/direct), and slow tests, including migration paths. Those need explicit focused coverage at final integration. Published test counts/timings are historical, not results of this audit. |

No missing authentication prerequisite blocks this work. The significant work is extending historical contracts consistently across storage/sync, adding deterministic aggregates, and reorganizing the desktop. Actual Windows layout and live PostgreSQL behavior are not verified by this audit.

## 2. Shared requirements and metric contract

### A. Identity and immutable history

Introduce an owner-scoped, stable `task_type_id` with a display label; category and tags remain independent. Use a minimal reusable type record and a task-editor create/select control. New standalone tasks get a distinct type unless the user explicitly selects an existing one. Recurring occurrences inherit their series' type; continued series inherit their predecessor's type. Migration derives deterministic type UUIDs from the oldest provable series root, or the standalone task ID, in a dedicated namespace. Never group unrelated same-name tasks automatically. This establishes a type identity distinct from occurrence identity without guessing semantic equivalence. Missing/invalid legacy chains remain explicitly unknown or separately grouped, never silently merged.

Snapshot type ID/label, task name, category, tags, task points and estimate on newly saved placements; keep optimizer score and optimization metadata separate. Preserve original snapshots through moves and regeneration lineage; each new placement may additionally record its current planning snapshot. Execution creation retains its established snapshot timing. Do not overwrite an existing execution's points because the task was edited. Retain tombstones and provenance. Backfill only facts actually recoverable from persisted records, with provenance; otherwise nullable/unknown. Never fabricate historical dates, points, completions or original labels from current mutable values.

An intended occurrence is one placement lineage linked by `superseded_by_id`; recurrence slots are separate occurrences even within one type. Reuse existing recurrence IDs and lineage rules. Multiple legitimate schedules of a nonrecurring task remain separate occurrences unless explicitly linked. Validate ownership, cycles and inconsistent chains. A depth limit must produce a visible incomplete-history result rather than a confident truncated award.

### B. Eligibility, status and reporting dates

Use flexible task occurrences, not fixed blocks, recurring series definitions or unscheduled backlog. Backlog may be shown separately but never lowers planned-work completion. The applicable placement at the explicit `as_of` instant selects the planned date and interval, using the existing cohort algorithm. Due means planned end <= `as_of`.

Due denominator = due completed + due skipped + due never-started + due in-progress + due paused. Cancelled, future and removed-without-successor occurrences are excluded and separately visible. Due completion = due completed / denominator; due skip = due skipped / denominator. Zero denominator is unavailable, not 0%. Unresolved = not-started + in-progress + paused; overdue-not-started is the due subset, not an additional disjoint count. Label overlapping breakdowns.

Moved work belongs to its applicable planned date exactly once. A move across the range leaves an explicit moved-out indication and history on the original date; removed work remains browsable with reason. Do not change this established accounting to secretly retain it in two denominators. Retain original and current plans so unfinished work cannot disappear without explanation.

Keep three labelled date bases: (1) existing execution statistics filtered by execution creation time; (2) follow-through/counts filtered by applicable planned date; (3) earned-point and completed-activity views filtered by recorded completion instant. Do not feed one denominator into another basis. Reading completion activity must find completions in the range even when their planned date is outside it, including work whose task/placement was later removed. Unknown completion dates are excluded from dated awards with an unknown count.

Use explicit IANA reporting timezone, Monday–Sunday calendar weeks, calendar months, inclusive local date selections implemented as half-open instant intervals. Today/week/month periods include their future planned work but only already-due work enters due rates. Convert stored instants through existing timezone helpers; never assume 24 elapsed hours per local day. Buckets follow planned local start: night 22:00–05:59, morning 06:00–11:59, afternoon 12:00–17:59, evening 18:00–21:59.

### C. Earned points, averages and achievements

Reuse execution `points`, not optimizer `score`. A live completed occurrence contributes its immutable execution-points snapshot exactly once. Zero is a valid known value; negatives are invalid. Missing legacy points remain unknown, not copied from today's task value. Show known-point subtotal and missing counts; incomplete history cannot establish an unqualified best-ever record. No award ledger is required if authoritative records and deduplication suffice.

Attribute points and completion activity to `actual_final_end_at` in the reporting timezone. Reopen withdraws the award; completing again contributes once at the new completion date with the retained points snapshot. Deleting execution history removes those awards and timed metrics. Deleting a task or removing a placement alone does not delete genuine recorded completion activity. Rebuild caches after mutations/sync; no UI counters as source of truth.

General awards: highest earned-point day, best completed calendar week, most frequently completed type (with that type's separately labelled due-completion rate), longest and current green-day streak. Return contributing record IDs, calculations, date ranges and sample counts for every card. Return all ties, sorted deterministically; show an earliest representative plus tie count when space is limited. Show current partial week separately from best complete week. Incomplete coverage receives a qualification or unavailable award.

For General daily/weekly averages, use elapsed calendar days from the first known planned-or-completed activity to yesterday within the selected range, including zero-work days. Completed whole Monday–Sunday weeks entirely inside that interval form the weekly denominator; none means n/a. Show current day/week separately as partial. Average completed tasks, points and productive active minutes over those days/weeks; also show timed and known-point coverage. Average daily due-completion rate uses only days with a nonzero due denominator and is labelled an unweighted daily average. Show pooled due completion separately. No history gives zero counts and unavailable averages/records.

Reuse `classify_day` for green streaks: both existing light/dark green classes qualify (>=60% scheduled completed; dark green >=80%). Keep thresholds centralized with the shared classifier; do not introduce an inconsistent 80% streak rule. This calendar classification includes cancelled as uncompleted and is explicitly distinct from the cancellation-excluding due metric. Empty days cannot earn green and break a calendar-day streak. Finalized past nongreen days break it. An unfinished current day does not break yesterday's current streak; extend through today only when all today's scheduled occurrences are resolved and the day is green. Show today as provisional otherwise. Calculate from the current authoritative historical outcome, so later reopen/deletion can revise a streak; do not promise immutable past awards. Never cap a longest streak at a chunk boundary.

Evidence thresholds reuse `ProductivityThresholds` (5/15/30 by default), calculated from the metric's own denominator. Most-supported slot means largest completed-duration sample, with low-evidence wording. Highest-completion weekday means highest due-completion rate among supported weekdays, with all ties and numerator/denominator. Highest-points weekday shows both total and average; rank by average points per elapsed eligible calendar weekday, including zero-point dates, and state that basis. Don't imply causes or model predictions.

### D. UI feature inventory (exactly three primary sections)

**General:** clickable awards above; daily/weekly averages; explicit completed/skipped/cancelled/overdue/unresolved counts; productive minutes, signed median start delay (negative = early) and mean absolute duration error. Preserve completion-among-resolved = completed / (completed + skipped + cancelled), clearly labelled execution-based. Fix skipped with an actual status count, never subtraction of rounded rates.

**Task-based:** compact type list/table with today/current week/current month/all-time periods; planned/completed/skipped/cancelled/overdue/unresolved counts; due rate with parts; known earned points, productive minutes, median estimated/actual durations and MAE; supported weekday/bucket evidence; selected-type history drill-down. Snapshot category/tag filtering must not merge type identities.

**Time-based:** daily completion/points view and day drill-down, weekly/monthly summaries; weekday rates and points totals/averages; planned-start bucket rates; highest-completion/highest-points weekday; supported slots per category/type; recent seven-day trend vs selected range; planned-vs-actual category/type chart; existing rule-based insights with evidence/sample counts.

Keep main filters (All time/7/30/90 days, category, tag, weekday, planned-start bucket), Apply, Clear, selected-filter label. Preserve independent follow-through 7/30/90-day selector and independent history date/status/category selectors. Main-filter changes must not silently alter those independent scopes. Put follow-through inside Time-based and history/data in shared detail/tools panels, not extra primary tabs.

Retain every follow-through detail: due rates/parts, never-started overdue, in-progress/paused, cancelled and future counts; due/future planned minutes; completed planned/actual minutes and missing actual count; signed median duration error, ratio and start timing; moved-occurrence share, total move events and regeneration replacements; unfinished-workload signals with reasons; consistent category underestimation; reporting timezone/range/as-of and unknown/removed/moved-out notes. Preserve signals as observations, not causes or known historical capacity.

History supports Completed/Skipped/Cancelled/In progress/Paused/Overdue/Upcoming/Removed. Detail shows historical name/category, original times/timezone, moves/replacements/removal reasons, estimate at execution creation, actual first/final instants, active time excluding pauses and each work session including open sessions. State when no execution exists. Historical names must not silently change after task edits.

Data tools explain device/synced-account/direct-server storage; export whole active-workspace execution history as CSV/JSON regardless of filters; preserve export compatibility; confirm history deletion with profile-specific effects and show success/errors. Deletion also removes derived achievements but does not delete plans: remaining due plans may become unattempted. Keep server tombstones and sync propagation. Reload on return; show loading, empty, insufficient/unknown and safe error states. Use background workers and stale-request/account guards, bounded/paged detail lists and existing charts/calendar components. No fake demo numbers in real reports.

### E. Persistence and compatibility

Use existing owner-scoped local repositories and authenticated HTTP sync for offline-first signed-in use. Persist source records and queue retry through the existing outbox. Extend normalized backend live/revision models, record mapping, snapshots, wire mapping and capabilities together. Older clients/servers must not silently erase new snapshots; incompatible pending records stay visible and queued. Keep association explicit, logout/account switching isolated, conflict choices versioned and deletions nonresurrecting. Preserve direct PostgreSQL with the same contracts/services, but no new offline replica or credentials distribution. Never read the project's real `.env` for verification.

All-time views must support >366 days using bounded queries/chunks and exact mergeable metrics (medians must not be approximated by medians of chunk medians), unique lineage/type aggregation, cross-boundary streaks and completion-date queries. Do not issue per-row database calls. Respect SQLite parameter limits and server query budgets. Report truncation/incomplete coverage instead of silently dropping older work.

## 3. Copyable implementation prompts

### Prompt 1 — durable types and historical snapshots

```text
Implement step 1 of docs/productivity-redesign-plan.md. Read AGENTS.md and the shared contract A–E; use its audit as navigation. This is implementation, not another planning response. Preserve existing user changes. Limit this step to identity, snapshots and lifecycle capture, including the storage parity necessary to keep both persistence profiles usable.

Start at app/planning/models.py, application.py:PlanningService, workflow.py, recurrence.py, repository.py, history.py; app/execution/models.py, service.py and db.py. Inspect directly affected creation/import/manual-move/regeneration callers and app/planning/csv_canonical.py. Backend parity paths: backend/models.py, record_mapping.py, snapshots.py, planning_repository.py, executions.py and migrations/versions. Append migrations from actual heads (audited local 11, backend 0012); never rewrite old migrations.

Implement stable reusable type records and ownership, deterministic legacy assignment and recurring-series inheritance per contract A. Add immutable planning snapshots and historical read-model support. Capture them atomically on all placement paths, retaining original lineage information and existing execution-points semantics. Preserve unknown legacy facts and provenance. Update live/revision mappings and additive serialization defaults; keep pending richer records from being silently downgraded until capability support in step 3. No unrelated task editor redesign yet.

Acceptance: restart preserves snapshots/types; renaming/category/tag/points edits do not rewrite historical snapshots; unattempted work has history; recurrence shares type but has distinct occurrences; moves/regeneration do not create duplicate logical work; mixed-account references are rejected; local/direct mapping round-trips and rollback are correct; supported older data migrates without invented history. Avoid production schema changes.

Use static checks first. Add only critical migration/round-trip/ownership regressions now; defer the larger matrix to step 5. If necessary run python -m pytest tests/execution/test_migration_v<N>.py and a specifically selected new backend migration/mapping test on disposable fixtures; replace <N> with the migration actually added. Do not run dev or full suites in this intermediate step. End with changed files, contract decisions, checks actually run and blockers for step 2. Record a concise handoff in this plan without rewriting unrelated content.
```

### Prompt 2 — deterministic tracker analytics and awards

```text
Implement step 2 of docs/productivity-redesign-plan.md after step 1. Read AGENTS.md, shared contract A–E and step 1's handoff. Build domain/service analytics, not UI calculations.

Start at app/productivity/reporting.py:ProductivityService, schedule_cohort.py, day_summary.py, stats.py, segments.py, trends.py, insights.py; app/planning/history.py and the local/backend history repository adapters. Inspect execution read APIs for completion-date queries independent of planned dates. Reuse existing TaskExecution.points and lifecycle.reopen semantics; never use optimizer scores as earned points.

Implement General achievements and drill-down references, daily/weekly averages, per-type four-period breakdowns, day/week/month views, weekday/bucket metrics and points. Follow every formula/date/streak/missing-data rule in the shared contract. Add actual completed/skipped/cancelled counts to the report contract and remove rounded-rate count reconstruction. Retain execution-created-date statistics and distinguish their basis from due and completion-date views. Preserve all existing follow-through and insight outputs. Keep most-supported slot distinct from highest performance.

Support all-time queries beyond 366 days with bounded reads, exact aggregate merging, lineage deduplication, cross-window streaks and explicit completeness. Include completed activity whose plan is outside the range or later removed. Reopen/deletion retract derived awards; re-completion earns once from the retained snapshot. No permanent incrementing counter or unnecessary award ledger.

Acceptance: pure calculations take explicit clock/timezone; ties and empty/unknown data are deterministic; planned, completed and unattempted work reconcile; no per-row storage queries or median-of-medians error; current partial day/week never appears as a completed historical record. Existing public reports remain compatible where practical.

Author minimal deterministic regression cases needed to validate tricky formulas. If uncertain, run only relevant cases such as python -m pytest tests/productivity/test_day_summary.py tests/productivity/test_schedule_cohort.py -k <specific-case>, substituting real node names. Defer broad tests to step 5. Report changed files, definitions settled, checks and remaining gaps in a concise handoff.
```

### Prompt 3 — reliable account synchronization and deletion

```text
Implement step 3 of docs/productivity-redesign-plan.md after steps 1–2. Read AGENTS.md, shared contract A–E and prior handoffs. Extend existing sync; do not redesign authentication or introduce database credentials into normal desktop distribution.

Start at app/sync/mapping.py, engine.py, store.py, service.py, transport.py; backend/sync.py, mutations.py, models.py, record_mapping.py, snapshots.py, executions.py; app/ui/app_services.py and direct_services.py. Inspect existing tests/sync/test_execution_history_sync.py, test_outcome_sync.py, test_rescheduling_sync.py and test_recurrence_sync.py only where relevant.

Carry type identities and all new historical source snapshots through dirty tracking, outbox, push/pull, normalized revisions, conflict resolution and related-record ordering. Implement capability negotiation so older peers cannot drop fields or accept incomplete history; incompatible operations remain pending with a useful message. Ensure retry and acknowledged revisions cannot overwrite concurrent local edits. Reuse existing type identity on both devices; sync dependencies atomically or in validated order.

Validate offline capture/restart/retry, two-device reconstruction of the same analytics, ownership checks, account switches and 401 reauthentication. Keep ownerless association explicit. Propagate execution-history tombstones, invalidate derived analytics and prohibit stale-device resurrection/duplicate point awards. Preserve current direct-server mode without adding an offline replica; verify its adapters carry identical historical fields and return equivalent analytics.

Acceptance: snapshots/types survive sync without mutation; duplicate pushes/moves/completions do not duplicate occurrences or awards; conflicts preserve a user's pending work; deleted history stays deleted; account A never leaks into B; read-only reporting makes no writes. Do not contact real accounts or use .env.

Add narrowly scoped protocol/ownership/deletion tests as needed; most integration cases belong in step 5. If behavior is uncertain run the specific new test node in tests/sync or tests/direct, not those whole directories. Do not run dev/full suites here. Report changed files, compatibility behavior, checks and blockers in a concise handoff.
```

### Prompt 4 — three-section desktop Productivity UI

```text
Implement step 4 of docs/productivity-redesign-plan.md after steps 1–3. Read AGENTS.md, shared contract especially inventory D and the previous handoffs. Build the native desktop UI using existing services and components; do not duplicate analytics in widgets.

Start at app/ui/productivity_page.py, productivity_controller.py, productivity_charts.py, cohort_view.py, history_model.py, background.py; inspect calendar_view.py/selected_day_panel.py for reusable day presentation and task_editor.py/task_form_model.py for a minimal type create/select control. Preserve app_services.py/direct_services.py ownership and worker lifecycle.

Present exactly General, Task-based and Time-based as primary sections. Implement all inventory D cards, charts, tables and interactive drill-downs. Keep independent history/follow-through filters, existing main filters, whole-workspace exports and profile-specific confirmed deletion accessible inside these sections or a shared detail/tools panel. Preserve every follow-through/history detail, insight and data-quality note. Fix the Skipped card using the actual count supplied by step 2. Show snapshot-based names, type identity separate from category/tags, metric date bases, denominators, samples, unknown coverage, partial periods and evidence.

Load lazily where useful, perform storage/aggregation off Tk, and use request/account-generation guards so old filters or an old account cannot overwrite new results. Keep large tables/history paginated or bounded, charts responsive, scrolling and resizing usable, keyboard navigation and both appearance modes working. Refresh on return and after successful sync/mutation/deletion. Empty/error states must be truthful; never fill with invented metrics.

Acceptance: every award/type/day opens the correct contributing records/calculation; four type periods work; independent filters stay independent; Clear resets only its documented scope; repeated clicks and destroyed widgets are safe; data remains private on account switch; task-type creation/selecting is available without broad task-form refactoring. Existing scheduling engines and calendar classifications retain behavior.

Use window-free controller/view-model checks first; run only a focused test if needed (python -m pytest tests/ui/test_productivity_controller.py -k <real-case>). Add UI test cases for step 5 and a short manual Windows checklist. Do not run broad suites here. Report changed files, checks, remaining visual/accessibility issues and a concise handoff.
```

### Prompt 5 — integration, development-suite verification and documentation

```text
Complete step 5 of docs/productivity-redesign-plan.md after steps 1–4. Read AGENTS.md, the shared contract/inventory and handoffs. Close missing requirements and regressions; this is the final integration step, not just a test report. Preserve user changes and do not weaken meaningful tests.

Use existing tests/productivity, tests/planning, tests/execution, tests/ui/test_productivity_controller.py and targeted backend/sync/direct modules. Follow docs/testing.md and tests/test_tiers.py. Add the bulk of regression coverage now using deterministic clocks, isolated databases and established fixtures. Cover:
1. All statuses, cancelled-vs-skipped, both completion formulas, unknown/missing/zero data, unattempted due work, open/paused sessions and untimed completions.
2. Type identity/grouping and all four periods, same-name distinct tasks, series continuation, renamed/deleted tasks, historical category/tag/points preservation.
3. Points once per occurrence, late completions outside planned range, removed plans with real completions, reopen/re-complete and deletion reversals, ties, averages, complete/partial weeks, green streaks and empty days.
4. Timezone/DST and calendar boundaries, bucket boundaries, cross-range moves, regeneration, recurrence, >366-day history, cross-chunk streaks, exact medians, lineage-limit diagnostics and bounded query counts.
5. Fresh/legacy migration, rollback/restart, local/backend normalized revision round-trip, capability negotiation, offline retry, idempotency, concurrent edits/conflicts, explicit association, logout/account isolation, stale-device deletion without resurrection and direct-profile parity.
6. Exactly three primary sections, drill-down contributors, independent filters, reload-on-return, whole-workspace CSV/JSON, deletion confirmations, loading/empty/error states, worker-only storage and stale-result guards.

Run python -m pytest -m dev once after implementation/tests are ready. This excludes important integration paths: additionally run only the changed migration, sync, direct and real-window test files needed for this feature, explicitly listing those files/results. Do not claim dev verifies them. On a disposable PostgreSQL database, exercise changed backend/direct contracts if one is provided; never substitute the project's real DATABASE_URL. If unavailable, report PostgreSQL unverified with the exact intended command/environment prerequisites.

For failures, identify change-related vs pre-existing failures, fix only relevant defects, run focused failing tests, then rerun the affected suite once after fixes. No full pytest by default: this task explicitly concentrates final verification on the development suite plus selected excluded integration tests. Run broader release/full-suite checks only if separately requested. Use targeted compile/lint checks for modified modules as appropriate.

Manually inspect Windows General awards/details, type selection and all four periods, time charts/day details, history filters/export/cancel-delete/confirmed-delete on disposable data, resizing/scrolling/keyboard/light-dark modes, account switching during load and restart persistence. If a real-window check cannot run, mark it unverified rather than passed. Document metric formulas, date bases, thresholds, historical gaps, type migration, storage profiles and deletion effects in existing analytics/UI docs. Summarize changed files, actual command outcomes and meaningful remaining limitations; complete the feature coverage checklist in this plan.
```

## 4. Coverage map

| Requirement | Implemented by prompts |
|---|---|
| Stable task types, recurrence grouping, original snapshots and migration | 1; sync 3; selector 4; verification 5 |
| General trophies, point records, streaks, averages and drill-down evidence | 2, 4, 5 |
| Task-based periods/counts/rates/points/durations/slots/history | 1, 2, 4, 5 |
| Daily/weekly/monthly views, weekday/bucket comparisons and trends/charts | 2, 4, 5 |
| Existing summary cards, skipped fix, evidence and insights | 2, 4, 5 |
| Full follow-through, never-started work, moves/removals and data-quality notes | 1, 2, 4, 5 |
| Full history, independent filters, export and deletion | 1–5 |
| Local-first per-user persistence, authenticated sync and no resurrection | 1, 3, 5 |
| Direct PostgreSQL compatibility without changing its storage model | 1–3, 5 |
| All-time scalability, timezone correctness, truthful empty/unknown states | 2, 4, 5 |
| Responsive native UI, three primary sections, account/request guards | 4, 5 |
| Development suite plus focused excluded integration coverage | 5 |

## 5. Preparation verification and handoffs

Planning-only preparation: read current code and test-tier configuration; cross-checked the inventory and references. No application code or database changed. No runtime tests were run because this request generates implementation prompts only. The implementation prompts reserve development/integration verification for step 5.

Implementation handoffs:

### Step 1 handoff — durable types and historical snapshots (2026-10-04)

**What exists now**

- `app/planning/models.py`: `TaskType` (owner-scoped id + label), `Task.task_type_id`, `derived_task_type_id(root)` (uuid5 in `TASK_TYPE_NAMESPACE`), and the placement planning snapshot on `ScheduledTask`: `task_category` (existing) plus `task_name`, `task_tags`, `task_points`, `task_estimate_minutes`, `task_type_id`, `task_type_label` (`PLACEMENT_SNAPSHOT_FIELDS`, `placement_snapshot`). Each is `None` when not recorded; `[]` tags and `0` points are known values.
- `PlanningService`: `list_task_types` / `get_task_types` / `create_task_type` / `update_task_type` (rename), `placement_snapshots(tasks)`. Types are resolved in `_resolve_task_types` on every task save and CSV batch; snapshots are captured in `_replace_range` (generation, regeneration, replace), `apply_reschedule` (manual move), `workflow._with_category_snapshots` (incremental generation) and `_prepare_batch_history` (import).
- `app/planning/history.py:historical_plan(placement, execution=None, task=None)` returns the historical name/category/tags/points/estimate/type with a per-fact source (`placement_snapshot`, `execution_snapshot`, `current_task` for type identity only, `unknown`). `app/ui/history_model.py` now prefers the snapshot name.
- Storage: local schema **v12** (`_migrate_v11_to_v12`), backend Alembic **0013**. Backend adds `task_types` + `task_type_revisions`, `tasks.task_type_id` (composite FK to the same user's types), snapshot columns on `placements`/`placement_revisions`, tag snapshot child tables, a `TASK_TYPES` resource (`/task-types`), and `task_type` as a revision entity type.

**Contract decisions**

- Default type = `derived_task_type_id` of the task's own id, or of the oldest provable series root (occurrence → series → predecessor chain; the walk stops at a missing, foreign-owned or cyclic link, so a broken chain is grouped on its own). Names are never compared. Both migrations and the service use the same rule, so devices and server derive identical ids.
- An occurrence always takes its series' type. A save that omits the type keeps the stored one; a type is never cleared. An explicitly chosen type must be a live type in the same owner scope.
- Snapshots are immutable: a kept placement keeps its stored snapshot (including an unknown one); a move/regeneration tombstone keeps the original and the replacement records the task as it is then. Lineage is unchanged (`superseded_by_id`).
- Migrations assign types to existing tasks (tombstones included) with no version bump, no revision/change-log entry and no sync dirty mark. Type labels are the root's name at migration time, timestamped at migration. Existing placements keep NULL snapshots; nothing is back-filled from current tasks. Old task revisions keep a NULL type.
- Execution schema and execution-points semantics are untouched. Type and snapshot fields are excluded from the scheduling inputs fingerprint, so no saved schedule became stale.
- Until step 3: the sync client neither sends types nor snapshots. A pulled record never replaces a locally recorded type or snapshot field (local non-null wins, else the server's value). Task types are not in `SYNC_TABLES` (no dirty capture, no outbox), but `associate_local_data` claims ownerless types with their tasks and an insert trigger stamps the active account. The server keeps a stored type/snapshot when a payload omits it, rejects a changed snapshot value (422), and `/changes` hides `task_type` entries unless `include_task_types=true`.

**Checks actually run** (project venv, Python 3.10; no dev or full suite)

- New: `tests/execution/test_migration_v12.py`, `tests/planning/test_task_types_and_snapshots.py`, `tests/direct/test_direct_task_types.py` — pass.
- Existing files touched by the storage/wire change: `tests/backend/test_migrations.py`, `test_normalized_migration.py`, `test_normalized_storage.py`, `test_change_log.py`, `test_resources.py`, `test_planning_api.py`, `test_sync_push.py`, `test_recurrence_api.py`, `test_reschedule_api.py`, `test_task_data_reset.py`; `tests/sync/test_recurrence_sync.py`, `test_rescheduling_sync.py`, `test_task_data_reset_sync.py`; `tests/planning/test_explicit_rescheduling.py`, `test_recurrence_changes.py`, `test_recurrence_csv.py`; `tests/execution/test_migration_v9.py`, `test_migration_v10.py`; `tests/ui/test_data_compatibility.py` — pass after updating three test helpers that pin the old shape (`as_of_head`, `ALLOWED_TEXT`, the pre-v8 writer emulation).
- `ruff check` and `compileall` on the changed modules — clean. PostgreSQL not exercised (SQLite server schema only); `tests/web` and the rest of `tests/sync`, `tests/ui` not run.

**Open items for later steps**

- Step 2: use `historical_plan` for names/categories/types; legacy placements have unknown name/tags/points and only a `current_task` type identity. An execution without a placement has no type snapshot (type comes from its task today). `historical_plan` leaves tags unknown when only an execution snapshot exists (the execution stores a single `tag`).
- Step 3: add `task_type` to the sync protocol (client `ENTITY_ORDER`/`SYNC_TABLES` capture + a one-time dirty mark of existing local types, server `SPECS`/`SyncOperationIn`, capability flag, turn on `include_task_types`). A type create whose id already exists server-side must converge (both sides derived it), not conflict. Send `task_type_id` and snapshot fields; a placement the server created through the `reschedule` action or hosted generation already has a server-side snapshot that may differ from the device's, and the server rejects a changed snapshot. Server-side checks that an occurrence's type equals its series' are not enforced for raw REST/sync writes.
- Step 4: the task editor needs the create/select control; changing a series' type does not yet push the new type to already materialized occurrences until they are next saved.
- Canonical planning CSV carries neither types nor snapshots: an import assigns default (derived) types and snapshots live placements at import time; imported tombstones stay unknown. Local "Reset All Task Data" deletes the workspace's types; the server reset leaves type rows live.

### Step 2 handoff — deterministic tracker analytics and awards (2026-10-04)

**What exists now**

- `app/productivity/tracker.py`: pure `build_tracker_report(TrackerData, timezone_name=, as_of=, range_days=, filters=, thresholds=)` returning `TrackerReport` with `general` (counts, activity, durations, start delay, five awards, averages), `types` (one `TypeView` per type with `today` / `week` / `month` / `all_time` periods) and `time` (days, weeks, months, weekdays, buckets, ranked weekdays, supported slots, planned-vs-actual, recent 7 days vs range), plus `completeness`. `read_tracker_data(source, …)` loads the history.
- `ProductivityService.build_tracker_report(range_days=None, filters=None, timezone_name=None, as_of=None)` is the service entry point (same history source and reporting timezone as the cohort report).
- New read primitives on both planning repositories and `PlanningService`: `completion_history(start_utc, end_utc)` (live completed executions by recorded completion instant, with their placements, forward lineage, tasks and type records) and `history_bounds()`. `ScheduleHistory` now also carries `task_types` and `lineage_truncated`.
- `SegmentStats` gained `completed_count` and `skipped_count` (actual counts; `cancelled_count` already existed). The dashboard exposes them through `global_stats` and every segment.

**Definitions settled**

- Bases: counts and due rates use the applicable planned local date; points and completed activity use the local date of `actual_final_end_at`; the execution-created statistics stay in `generate_report` / `build_dashboard` and are not mixed in.
- `StatusCounts`: due denominator = due completed + skipped + not started + in progress + paused. `unresolved` and `overdue_not_started` are overlapping views. A zero denominator gives `value=None` with a reason.
- Points come only from the execution's `points` snapshot. A completion without one is counted with unknown points; affected awards are `qualified` with the reason. One completion per occurrence: completions are keyed by the last placement of their lineage (or by execution when unplaced) and the latest wins.
- Highest-point day ranks finished local dates only; today is returned as `partial`. Best week ranks whole Monday–Sunday weeks that lie inside [first activity, yesterday] by known points; the current week is `partial`. Most-completed type ranks by completion count and carries that type's due-completion rate in `detail`. All ties are returned, earliest (or alphabetical) first.
- Green streaks reuse `classify_day` (both green classes). Empty days and finished non-green days end a run. Today is provisional while it has no occurrences or any unresolved one. The longest streak is searched in the selected range; the current streak always uses full history.
- Averages: elapsed calendar days from the first planned-or-completed activity in the range through yesterday, zero-work days included; weekly uses complete weeks only (`None` when there is none). The daily due-completion average is unweighted over days with a non-zero denominator; the pooled rate is separate.
- Highest-completion weekday requires at least `thresholds.low` due occurrences per weekday. Highest-points weekday ranks by known points per elapsed eligible calendar date of that weekday. Supported slot = planned-start bucket with the most timed completions, labelled as evidence, not performance.
- All-time: history is read in windows of at most 366 days (bounds query, then one planned read and one completion read per window), raw records are merged, so medians are exact. `MAX_HISTORY_DAYS` (30 × 366) caps the read and sets `history_truncated`. A lineage longer than 64 steps sets `lineage_truncated`. Both mark the report incomplete.
- Main filters (category, tag, weekday, planned-start bucket) filter records by their snapshots and never regroup types. Unknown types are grouped under `type_id=None`.

**Checks actually run**

- New: `tests/productivity/test_tracker.py` (9 cases) and `tests/direct/test_direct_task_types.py::test_the_tracker_reads_completion_activity_from_the_server_schema` — pass.
- Existing: `tests/productivity/test_stats.py`, `test_reporting.py`, `test_schedule_cohort.py`, `test_schedule_cohort_service.py`, `tests/direct/test_schedule_cohort_adapters.py` — pass. `ruff check` clean on the changed modules.

**Remaining gaps**

- No HTTP route serves the tracker report; the hosted web profile cannot show it yet. The direct and local profiles can.
- Best week is ranked by known points (the contract did not name the measure); completed count is in each winner's `detail`.
- Time-of-day buckets use each plan's own timezone, as the cohort report does; dates use the reporting timezone.
- A day whose only history is a removed placement does not appear in `time.days` unless it lies after the first remaining activity; removals are totalled in `completeness.removed_from_plan`. `moved_out` is counted per day when the successor is on another date or outside the read window.
- The existing cohort report still caps at 366 days by itself; only the tracker reads further.
- SQLite finds completions with `datetime(actual_final_end_at)` (no index): fine at personal scale, a candidate for a UTC twin column if it becomes slow.

### Step 3 handoff — synchronization of types and snapshots (2026-10-04)

**What changed**

- Local schema **v13**: change-capture and owner triggers for `task_types`, and every existing type marked for upload once. `SYNC_TABLES` now includes `("task_type", "task_types")`; the v5/v11 migrations keep using a frozen list.
- Client (`app/sync`): `task_type` is a synchronized record (sent before tasks). Task payloads carry `task_type_id`; placement payloads and the reschedule action carry the planning snapshot. `pull` always asks for `include_task_types=true`.
- Server (`backend/sync.py`, `mutations.py`): `task_type` operations, the `"task_types"` feature in `/sync/capabilities`, snapshot fields on the reschedule action, and a `task_type` create whose id already exists converges on the stored record instead of conflicting.
- `workflow.reschedule_placement(..., snapshot=)` lets a synchronized move store the device's snapshot on the replacement.

**Compatibility behaviour**

- Server without `"task_types"`: type records stay dirty and are counted in `SyncReport.held` with a message; tasks and placements are sent without the new fields (as before). Nothing recorded locally is cleared by the server's answer. When the server later announces the feature, `_requeue_history_fields` marks, once per account, every task/placement whose acknowledged server copy lacks a field the device has, and they are uploaded.
- Older client against the new server: omitted type/snapshot fields keep the stored values; `/changes` hides `task_type` entries unless asked.
- Pulled records: a task takes the server's type when it has one, otherwise keeps the local one. A snapshot field recorded locally is never replaced; an unknown one takes the server's. A placement update repeats the server's value for any snapshot field the server already holds, so it is never refused as a changed snapshot.
- Task-type records are synchronized but not counted in the user-facing numbers (pending, pushed, pulled, association preview): those count the records the user made.
- Local "Reset All Task Data" removes local types and their pending marks; the server keeps its type rows live.
- Direct-server mode is unchanged (no offline replica); it uses the same service and repositories, covered by `tests/direct/test_direct_task_types.py`.

**Checks actually run**

- New `tests/sync/test_task_type_sync.py` (two devices reconstruct identical types, snapshots and tracker output; deleted history withdraws points on both and stays deleted; older-server hold and later upload) — pass.
- All of `tests/sync` (83 tests) — pass, after updating count expectations in `test_protocol.py` and `test_recurrence_sync.py`.
- `tests/backend`: `test_sync_push.py`, `test_change_log.py`, `test_reschedule_api.py`, `test_resources.py`, `test_migrations.py`, `test_recurrence_api.py`, `test_manual_placements_api.py`; `tests/execution/test_migration_v12.py`; `tests/ui/test_data_compatibility.py`; `tests/direct/test_direct_task_types.py`; `tests/planning/test_task_types_and_snapshots.py` — pass. `ruff check app backend` clean.

**Open items**

- A converged type create returns the server's label; the device keeps its own label until the type is next edited or pulled.
- The server does not enforce that an occurrence's type equals its series' for raw REST/sync writes (the shared service does).
- No new test for 401 re-authentication or account switching specific to types; the existing account tests in `tests/sync` still pass with types in play.
- PostgreSQL not exercised.

### Step 4 handoff — three-section desktop Productivity UI (2026-10-04)

**What changed**

- `app/ui/productivity_page.py` is rebuilt around a section bar with exactly three primary sections: **General** (five clickable awards with a detail box, status tiles, averages, notes on what is counted), **Task-based** (four period buttons, a paged type table, a type selector and its detail/history) and **Time-based** (paged day table with day detail, weeks/months, weekdays and planned-start comparison, two charts, supported slots, recent trend, insights, and the schedule follow-through card). The main filters sit above the sections; the History and Data panels sit below them as shared tools.
- `app/ui/tracker_view.py` (Tk-free) words the tracker report; the page calculates nothing. `ProductivityController.build_tracker(range_days, filters)` is the only new data call.
- `TrackerReport.records` / `type_labels` give every id in the report a name, date, state and points for the drill-downs.
- Task form: a "Task type" select under More options (`(its own type)`, the workspace's types, `New type...` with a name field). `TaskDraft.task_type_id` / `new_type_label`, `EditorOptions.task_types`, `PlanningController.list_task_types` / `create_task_type`.

**Behaviour to know**

- The Skipped tile is the actual skipped count of planned occurrences; "Completion among resolved" shows the execution-based rate with its actual counts from the dashboard.
- Each request kind (tracker, dashboard, cohort, history) carries a number; an older answer is dropped. The existing background runner still drops results for a destroyed page or a previous account.
- Clear resets the main filters only. The follow-through window and the history date/status/category selectors are independent of them and of each other.
- The category and tag filter lists come from the execution-based dashboard; weekday and planned-start lists are fixed.
- Charts: planned-vs-actual switches between category and task type; the bucket chart now shows due completion by planned start (planned-date basis), not the execution-based rate.

**Checks actually run**

- New: `tests/ui/test_tracker_view.py` (window-free, 6 cases) and `tests/ui/test_productivity_page.py` (real window) — pass.
- Existing: `tests/ui/test_productivity_controller.py`, `test_task_form_model.py`, `test_task_editor_widgets.py`, `test_day_status_board.py`, `test_execution_workflow.py`, `test_recurrence_desktop.py`, `test_responsiveness_regressions.py`, `test_desktop_shell.py` — pass after one pinned change-capture count was updated. `ruff check app` clean.

**Manual Windows checklist (not performed by a person in this step)**

1. Productivity opens on General; the three section buttons switch content and keep their state when you return.
2. Each award button shows its calculation, ties, the current partial period and the contributing records.
3. Task-based: the four period buttons change the table; Previous/Next page; choosing a type shows its detail and history; two types with the same label are both selectable.
4. Time-based: page through days, pick a day, switch the duration chart between category and type; change the follow-through window.
5. Change main filters and Apply; Clear; confirm the follow-through window and history filters do not move.
6. History: each status filter; Data: export CSV and JSON, cancel the delete dialog, then confirm it on disposable data.
7. Resize narrow and wide, scroll the page and the tables, tab through the controls, switch light/dark.
8. Sign in / switch account while the page loads; restart and confirm the same figures.
9. Day page form: More options, Task type: keep own, pick an existing one, create a new one.

**Remaining visual/accessibility issues**

- The type and day tables are fixed-width text in a read-only box (keyboard scrollable, not cell-navigable); long type labels are cut at 28 characters in the table (full in the selector and detail).
- Award buttons are multi-line buttons; the selected one is shown by colour and by the detail title, not by a text mark.
- Text cards use fixed wrap widths (as the previous page did), so very narrow windows wrap unevenly.

### Step 5 handoff — integration, verification and documentation (2026-10-04)

**Closed in this step**

- A day whose only history is a moved or removed plan is now listed in `time.days` with its `moved_out` / `removed` count (all-time range), so the original date explains where the work went.
- New regression file `tests/productivity/test_tracker_history.py`: every status and both completion formulas, DST and reporting-timezone dating, move plus regeneration counted once, recurring occurrences under one type, renamed and deleted tasks, exact medians across reading windows, the lineage-limit diagnostic, owner isolation.
- Docs: `docs/analytics.md` section 5 (date bases, formulas, thresholds, task types and their migration, completeness and historical gaps, storage profiles, deletion effects); `docs/desktop-task-form.md` (the Task type control).
- Older tests that pinned the previous shape were updated, not weakened: a stored task now carries its derived type, the table list includes `task_types`, migrations queue the derived type records, and a task create logs its type record too (`tests/execution/test_db.py`, `test_migration_v7/8/9/10.py`, `tests/planning/test_application_service.py`, `tests/ui/test_planning_controller.py`, `tests/direct/test_direct_services.py`).

**Commands actually run (project venv, Python 3.10, SQLite)**

| Command | Result |
| --- | --- |
| `python -m pytest -m dev` (first run) | 1621 passed, 9 failed, 2 skipped — all nine were pinned-shape expectations caused by this feature |
| `python -m pytest -m dev` (after fixes, once) | **1630 passed, 2 skipped, 317 deselected** |
| `python -m pytest tests/direct tests/web tests/sync/test_task_type_sync.py` | 157 passed, 2 failed (change-log counts in `test_direct_services.py`); after the fix `tests/direct/test_direct_services.py`: 11 passed. The whole selection was not rerun after that fix |
| `python -m pytest tests/sync` | 83 passed |
| `python -m pytest tests/ui -m ui` (real windows) | 47 passed |
| `ruff check .` | clean |

The dev suite does not cover the sync, web, direct and real-window tiers; those are the separate rows above. The full suite (`pytest` with the slow tier) and `python -m compileall .` were not run.

**Not verified**

- **PostgreSQL**: no disposable database was provided. Intended command, with a database whose name contains `test`:
  `BACKEND_TESTS_ON_POSTGRES=1 TEST_DATABASE_URL=postgresql://<user>:<password>@<host>/<name>_test python -m pytest tests/backend/test_migrations.py tests/backend/test_normalized_migration.py tests/direct/test_direct_task_types.py tests/direct/test_schedule_cohort_adapters.py tests/sync/test_task_type_sync.py`
- **Manual Windows inspection** (the checklist in the step 4 handoff): not performed by a person. The automated real-window tests cover section switching, award/type/day details, independent filters and the task-type control; visual layout, resizing, light/dark and account switching during load are unverified.
- The slow tier.

**Feature coverage checklist**

| Requirement | Status |
| --- | --- |
| Stable task types, recurrence grouping, original snapshots and migration | Done (v12/v13, 0013); tested locally and on the server schema (SQLite) |
| General trophies, point records, streaks, averages and drill-down evidence | Done |
| Task-based periods/counts/rates/points/durations/slots/history | Done |
| Daily/weekly/monthly views, weekday/bucket comparisons and trends/charts | Done |
| Existing summary cards, skipped fix, evidence and insights | Done (Skipped is an actual count; insights and completion-among-resolved kept) |
| Full follow-through, never-started work, moves/removals and data-quality notes | Done (follow-through card unchanged, inside Time-based) |
| Full history, independent filters, export and deletion | Done (history and data panels unchanged in behaviour) |
| Local-first per-user persistence, authenticated sync and no resurrection | Done; two-device test |
| Direct PostgreSQL compatibility without changing its storage model | Done on the server schema under SQLite; PostgreSQL itself unverified |
| All-time scalability, timezone correctness, truthful empty/unknown states | Done (bounded windows, exact medians, DST test, empty-state tests) |
| Responsive native UI, three primary sections, account/request guards | Done; visual checks unverified |
| Development suite plus focused excluded integration coverage | Done (table above) |

**Remaining limitations**

- No HTTP route serves the tracker report, so the hosted web profile cannot show the new sections.
- The canonical planning CSV carries neither task types nor planning snapshots.
- Placements saved before this feature have no recorded name, tags or points; legacy completions without a completion time are not dated.
- A task type converged between devices keeps each device's label until the type is next edited or pulled.
- The server does not enforce that an occurrence's type equals its series' for raw REST/sync writes.
- Changing a series' type reaches already materialized occurrences only when they are next saved.
- The type and day tables are fixed-width text, not cell-navigable grids.
