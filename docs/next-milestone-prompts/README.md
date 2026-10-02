# Repository audit and sequential Claude Code prompts

Audited 2026-09-30 against branch `main`, HEAD `ec072e7`. The working tree was clean at the initial inspection. Read both root `AGENTS.md` and `CLAUDE.md`; the former ends mid-sentence in its dependency section. This deliverable adds documentation only. No application changes, branch changes, commits, deployments, production access, or `.env` reads were performed.

This is a source-and-test audit, not a fresh test run. Test names below identify inspected coverage, not claimed passing results. Earlier completion reports are historical evidence only. The current checkout is authoritative over prototypes, old milestone reports, and stale module comments.

## Evidence table

| Requirement | Current behavior and evidence | Gap | Proposed change |
|---|---|---|---|
| Recurrence expansion | `app/planning/models.py:RecurrenceSpec` supports daily/weekly/monthly, interval, weekly weekdays, monthly day, and mutually exclusive count/end date. `app/planning/allocation.py` consumes concrete tasks; no expansion. | No anchor/timezone on the recurrence rule, occurrence entities, or exception lifecycle. | Prompt 1: explicit series anchor/timezone, deterministic concrete occurrence tasks, bounded expansion and exceptions. |
| Stable occurrence identity | `app/planning/occurrence.py:occurrence_key` uses `(task_id, None)` for ordinary work and `(template_id, planned_date)` for recurring templates. | Identity is tied to placement date; cross-date recurring moves are refused. | Prompt 1: immutable original recurrence slot distinct from placement; migrate historical template placements without rewriting execution snapshots. |
| Recurrence storage | Local normalized task recurrence columns/weekday rows in `app/execution/db.py`; server equivalent in `backend/models.py`, mapped by `backend/record_mapping.py`. Local v8 and Alembic 0008 add task points after the v7 provenance work. | Old docs citing v7/0007 are not current migration heads. | Extend normalized live/revision storage and serializers; allocate new migrations from the actual head. |
| Execution integrity | `app/execution/lifecycle.py`, `ExecutionService`, server `Mutator`, and direct adapters share lifecycle behavior. `tests/direct/test_execution_contract.py`; desktop workflow tests exercise actions and no writes on viewing. | Need recurrence-aware identity and review of superseded never-started executions. | Prompt 2: retain snapshots/sessions, make superseded attempts non-actionable, test all storage paths. |
| Manual placement preservation | `workflow.reschedule_placement` atomically tombstones source, creates replacement, cancels a scheduled execution. `test_a_move_then_a_regeneration_is_one_traceable_chain` explicitly expects full regeneration to replace the move. | No durable preserve/release intent. | Prompt 2: persistent manual override, default reservation, explicit release, actionable conflicts. |
| Atomic regeneration and scope | `workflow.generate_from` rereads the input fingerprint inside a transaction; `PlanningService.reschedule_range` checks placement versions. Protected statuses include in-progress, paused, completed, skipped, cancelled. Outside-range same-occurrence supersession exists. | Extend existing guards to new occurrences, exceptions, manual intent, history snapshot; avoid replacing outside-scope work. | Prompt 2: transactional scoped replacement and concurrency tests; Prompt 5 adds history-sensitive freshness. |
| Sync and retries | `SyncEngine`, `SyncStore`, `SyncService`, `backend/sync.py`: durable dirty/shadow/outbox/conflict tables, op-id replay, related-record move results, atomic pull/cursor handling, bounded backoff and auth-required state. Move replay and competing-device tests exist. | New records and compound series operations must participate; recovery must retain all involved intent/history. | Prompt 1 supplies basic wire/storage support; Prompt 3 hardens conflict and interrupted-operation recovery. |
| Conflict UI | `account_page.py`, `AccountController.conflict_view`, `SyncEngine.resolve` implement local/server comparison, accept-remote and keep-local. Keep-local advances the shadow precondition; remote tombstones/collisions forbid resurrection. | Extend comparisons and guarded resolution to series changes/manual overrides; test server changes after the displayed conflict. | Prompt 3 extends the current UI and protocol, without a replacement sync system. |
| Account recovery | `AccountService` uses Argon2id; `backend/security.py` issues HS256 JWTs; browser sessions store hashed random credentials with CSRF controls. | No complete password recovery/delivery/reset path or password-change JWT revocation mechanism in inspected account/auth paths. | Prompt 4: single-use recovery, delivery integration, UI, credential epoch/session invalidation. |
| Basic server protection | Owner-scoped services; page limits; push max 200; planning range guard; 5 MiB CSV check; 4096-byte optimization metadata bound. `create_app` installs routes/error handlers, not a rate-limit/host middleware stack. Direct storage has additional connection safeguards. | Need consistent request budgets, auth/recovery throttling, trusted-host/proxy/CORS policy and hosted timeouts. | Prompt 4 audits existing controls, fills gaps, uses a shared limiter and disposable cross-user tests. |
| Normal/ADHD | `generate_day_schedule` reads `OptimizerMode`; Normal wire value `precise_greedy`, ADHD `adhd_friendly`. ADHD: tasks over 30 minutes start on quarter hours; shorter tasks use minutes; bounded short-gap reward only when configured (default weight zero). | Only two objectives; enum name mixes strategy and mode; comments claiming unwired behavior are stale. | Prompt 5 separates objective from algorithm, preserves old selections and baseline placement behavior. |
| Reward accuracy | `calculate_task_score` combines priority/category/task multipliers, preferred time, neighbor relations and fragmentation; canonical output uses insertion scores and `compute_total_score`. Legacy `calculate_schedule_score` can simply reuse stored scores. Task `points` are productivity value, not scheduling reward. | Neighbor scores can be stale; Early/Night need schedule-level compactness; Catch-Up needs a separate outcome definition. | Prompt 5: explicit objective formulas, final canonical evaluator and bounded history bonus. |
| Analytics | `schedule_cohort.py` groups placement lineage and separates future/cancelled work; its due denominator includes unattempted work. `PlanningService.schedule_history` supplies bulk history. | Existing due noncompletion rate is unsuitable as an automatic Catch-Up failure rate. | Prompt 5 uses explicit completed/skipped outcomes, one observation per immutable occurrence, no missing-record failures. |
| Availability/breaks/time | `planning/time.py` rejects ambiguous/nonexistent local instants and offset-transition scheduling windows; ordinary same-day windows can end at next midnight. `preferences_model.py` labels minimum gap a soft preference. | Do not imply overnight/DST scheduling or mandatory gap rules already exist. | Preserve explicit unsupported-window errors; fixed break blocks remain hard constraints; new modes cannot remove breaks or shorten durations. |
| Desktop responsiveness | `WorkerRegistry`, `run_in_background`, workspace guards; `run_io(background=False)` still executes local calls on Tk. Paint adapters avoid nested idle dispatch; lifecycle code controls GC. Regression tests exist. | Added recurrence/history/storage work must avoid synchronous Tk paths. Latest validation says mitigated, not fully resolved. | All prompts use existing workers; Prompt 6 tests stale results/shutdown and specifies physical Windows checks. |

Actual paths to preserve: desktop Make Schedule → `SchedulePageController.make_schedule` → `PlanningController.schedule_range` → shared workflow → allocation/day engine → transactional `reschedule_range`; Day/Allocation generation also uses the workflow. REST `/planning/generate` calls that workflow through `backend/planning_api.py`. Explicit moves use `PlanningController.reschedule_placement` → `workflow.reschedule_placement` → `PlanningService.apply_reschedule`; server/direct adapters reuse domain behavior. Desktop account UI calls `SyncService`/`SyncEngine`; server sync applies mutations through the existing mutator and operation log.

Earlier milestones already supply normalized storage, ownership, versioning, execution lineage, analytics, desktop rescheduling, conflict UI and responsiveness mitigations. Preserve these; do not reimplement them. Relevant stale statements include the mode enum's old “not wired” docstring, sync service's old “no screen” comment, and `execution-rescheduling.md`'s “no moving UI” limitation. No unrelated roadmap work is included.

## Run these prompts strictly in order

Each file is independently copyable, with its own boundaries, verification, and completion report. Complete and verify one before starting the next.

1. [Recurrence expansion and lifecycle](01-recurrence.md)
2. [Execution and manual placement preservation](02-execution-regeneration.md)
3. [Synchronization conflicts and recovery](03-sync-recovery.md)
4. [Account recovery and server protections](04-account-security.md)
5. [Shared scoring and five scheduling modes](05-scheduling-modes.md)
6. [Integration, comparison and documentation](06-integration.md)

## Security references checked

Recovery guidance was checked against [OWASP Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html): generic responses, protected expiring single-use credentials, trusted reset links and session handling. Proxy trust was checked against [FastAPI Behind a Proxy](https://fastapi.tiangolo.com/advanced/behind-a-proxy/): forwarded headers require explicit trust configuration. Direct retrieval of Starlette middleware and Uvicorn settings documentation failed during this audit; Prompt 4 requires checking their current official documentation before choosing framework-specific controls. No deployment configuration was changed.

## Requirement-to-prompt checklist

| Milestone requirement | Primary prompt | Integration proof |
|---|---:|---:|
| Bounded daily/weekly/monthly expansion; intervals/selectors/count/end | 1 | 6 |
| Original-slot identity, uniqueness, restart/retry/two-device deduplication | 1, 3 | 6 |
| DST, timezone, month-end, leap-year policies | 1 | 6 |
| This occurrence / future / entire-series edit-delete, synchronized exceptions | 1, 3 | 6 |
| Independent placement/execution; dependency semantics; protected series history | 1, 2 | 6 |
| Start/pause/resume/finish/skip/cancel/reschedule history | 2 | 6 |
| Persistent manual intent, release action, preservation/conflict reporting | 2 | 6 |
| Atomic stale-safe regeneration and outside-range policy | 2 | 6 |
| Durable outbox/replay/tombstones; both conflict versions; guarded keep-local | 3 | 6 |
| Interrupted sync, expired auth, offline retries, account switching | 3, 4 | 6 |
| Recovery request/delivery/UI/reset, expiry/reuse/races/session revocation | 4 | 6 |
| Ownership, payload/range/work limits, shared rate limiting, host/proxy/CORS/errors/secrets | 4 | 6 |
| Normal, Early Finish, Night Owl, ADHD, Catch-Up; formulas/ties/compactness | 5 | 6 |
| Historical evidence rules, bounded bonus, bulk reads, explanations, no ML | 5 | 6 |
| Mode persistence/sync/backward compatibility and all generation/UI paths | 5 | 6 |
| Final baseline/bonus recomputation; minute precision and hard constraints | 5 | 6 |
| Worker architecture, window movement/resizing regression protection | 1–5 | 6 |
| Identical-input pre-change comparison and complete requested metrics | 1 baseline capture; 5 comparison | 6 |
| Existing tooling, migration compatibility, sequential completion reports | Every prompt | 6 |

No test outcomes or physical Windows validation are claimed by this audit.
