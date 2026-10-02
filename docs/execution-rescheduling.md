# Execution, rescheduling and placement provenance (Milestone 5, session 1)

This is the contract for how planned work, attempts and changes to the plan
are recorded, on every storage path: the desktop's SQLite store
(`app/execution`, `app/planning`), the hosted API (`backend/`), the direct
PostgreSQL mode (`app/persistence`) and synchronization (`app/sync`,
`backend/sync.py`). Section 9 is the read contract for analytics.

## 1. Three records, three kinds of change

| Record | Meaning | Changes by |
| --- | --- | --- |
| `Task` | Intent: what should be done (name, category, estimate, rules). | Edits, deletion. |
| `ScheduledTask` (a *placement*) | Plan: one occurrence of a task in one interval. | Generation, **explicit reschedule**, deletion, reset. |
| `TaskExecution` (+ work sessions) | Attempt: what actually happened for one placement (or task). | Lifecycle actions, feedback, deletion. |

Keep these three operations apart:

- **Rescheduling a plan** moves a placement that has not been worked on to a
  new interval. The old placement becomes a tombstone that keeps its planned
  values and names its replacement. Nothing is deleted.
- **Cancelling an attempt** (`cancel`) is a lifecycle action on an execution:
  the attempt will not be (further) worked on. Its sessions stay.
- **Deleting data** tombstones a record so it leaves every normal read
  (placements, executions) or, for `reset_all_history`, purges local history.
  Deletion is not a plan change and not an attempt outcome.

## 2. The execution lifecycle

Defined once in `app/execution/lifecycle.py` and applied by the SQLite
`ExecutionService`, the server `Mutator` (REST, sync, direct mode):

| Action | From | To |
| --- | --- | --- |
| `start` | scheduled | in_progress |
| `pause` | in_progress | paused |
| `resume` | paused | in_progress |
| `complete` | scheduled, in_progress, paused | completed |
| `skip` | scheduled, in_progress, paused | skipped |
| `cancel` | scheduled, in_progress, paused | cancelled |
| `reopen` | completed, skipped | scheduled (paused when it has work sessions) |

Completed, skipped and cancelled are terminal for work: only `reopen` leaves
completed or skipped (the Day board's "back to Tasks"), keeping every session
and `actual_first_start_at` and withdrawing only `actual_final_end_at` and the
completion metrics, which the next `complete` recomputes; the server's change
log keeps the finished revision. `complete` from `scheduled` is a completion
reported without timing: no sessions, so the duration metrics stay unknown
(`null`), never 0. Every other action out of a terminal status is
refused (`InvalidTransitionError` / `409 invalid_transition`) and changes
nothing. Invariants, verified on every path by
`tests/direct/test_execution_contract.py`:

- `start`/`resume` open a session; `pause`/`complete`/`skip`/`cancel` close
  the open one. Time while paused is never in a session.
- `actual_first_start_at` is set by the first `start` only.
- `actual_final_end_at` is set by the terminal action.
- Only `complete` computes `actual_active_duration_minutes` (the sum of
  closed sessions), variance and start delay. A skipped or cancelled
  execution keeps its sessions but is **not** a completed duration sample.
- Each action is one logical mutation: version + 1, whatever it writes.
- The planned snapshot (name, category, estimate, planned date/instants,
  task and placement ids) is immutable.

## 3. The explicit reschedule

Entry points (one implementation, `app.planning.workflow.reschedule_placement`):

- Python (desktop, local web profile, direct mode): `workflow.reschedule_placement(service, placement_id, expected_version=..., planned_date=..., timezone_name=..., planned_start=..., planned_end=..., replacement_id=None)`.
- REST (hosted and local profiles): `POST /planning/placements/{id}/reschedule`
  with `{base_version, planned_date, timezone, planned_start, planned_end, replacement_id?}`,
  answering `{previous, replacement, cancelled_execution_id}`.
- Sync: the placement action `reschedule` (section 7).

### Legal source state

| Source | Result |
| --- | --- |
| A live placement at `expected_version`, of a live task, with no execution or a `scheduled` (never started) one | Moved. |
| Stale version | `VersionConflictError` / `409 version_conflict` |
| Already moved or deleted (a tombstone) | `VersionConflictError(deleted=True)` / `409 deleted` |
| Its execution is `in_progress`, `paused`, `completed`, `skipped` or `cancelled` | `HistoryProtectedError` / `409 history_protected` (`reason` = the status) |
| Its task was deleted | `InvalidReferenceError` / `422 invalid_reference` |
| Not found, or another owner's | `EntityNotFoundError` / `404` |

Started, paused and terminal attempts are never moved: that would rewrite
the history of an attempt. To try such work again, plan a new occurrence.

### Destination rules

The destination is validated inside the same transaction, against what is
stored then, with the rules the day engine and incremental generation use
(`workflow.reschedule_problems`, reusing `_kept_problems`). Every problem is
reported (`RescheduleRejectedError` / `409 reschedule_rejected`, `reason` =
the first, `problems` = all):

- `wrong_date`, `not_whole_minutes`, `unchanged` (it is already there);
- `recurring_occurrence_date` -- a *legacy* placement of a series
  definition (saved before recurrence was expanded) is the occurrence of its
  date (`app/planning/occurrence.py`); it can move only within that date. An
  occurrence of a series moves to any date and keeps its slot identity: the
  move re-dates the occurrence in the same transaction
  ([recurrence.md](recurrence.md)). `occurrence_taken` -- the occurrence (or
  the legacy template on that date) already has another live placement;
- `required_date`, `deadline_missed`, `task_out_of_range`;
- `outside_day_window`, `unsupported_day_window`, `overlaps_fixed_block`,
  `overlaps_placement` (the date's other live placements);
- `duration_changed` -- a placement lasts exactly its task's estimate;
- `engine_mode_changed` -- the adhd_friendly quarter-hour start rule;
- `dependency_not_satisfied` -- a dependency must finish first;
- `dependent_starts_first` -- a live placement of a task that depends on it
  would start before it ends.

A rejected move changes nothing (no row, version, change-log entry or local
change-capture mark).

### Effects, in one transaction

1. The placement becomes a tombstone: `removal_reason = "rescheduled"`,
   `superseded_by_id` = the replacement; its planned date, timezone, start,
   end, score and `task_category` stay as they were. (Written first, so a
   reader of the change order never sees both live.)
2. The replacement is inserted: the same task and occurrence, the new
   interval, a new id (`replacement_id` or a fresh UUID -- never a reused
   one, not even a tombstone's), `task_category` = the task's category now,
   `origin = "manual"` and `preserved = true` (the user's manual intent, see
   below).
3. The old placement's `scheduled` execution, if any, is **cancelled** (the
   lifecycle's `cancel`, `actual_final_end_at` = the time of the move,
   `cancel_reason = "rescheduled"`), so it is not actionable as current
   work. It keeps its snapshot and its link to the old placement.
4. Versions and change capture advance for exactly these records.

The dates' saved schedules become stale (their placements changed); nothing
is regenerated. Every later generation -- full or incremental -- keeps the
moved placement until the user releases it.

### Manual placements (Milestone 6)

Every placement records how it came to be and whether the user's intent
holds it in place:

| Field | Meaning |
|---|---|
| `origin` | `generated` (saved by a generation or a plain create) or `manual` (the destination of a move). `null` = unknown: saved before origins existed. A known origin never changes. |
| `preserved` | `true` while the user's manual intent holds: every generation keeps the placement exactly where it is. Only a `manual` placement can be preserved (a database CHECK). |

- **Set by a move only.** No update can grant intent; an update can only
  release it (`true` -> `false`). An update that omits both fields (an
  older client) keeps the stored values.
- **Released explicitly**: `PlanningService.release_manual_placement(id,
  expected_version=...)`, `POST /planning/placements/{id}/release`, or the
  Day timeline's "Release manual placement". It is one version-checked
  mutation (version + 1, `preserved = false`, origin `manual`) that moves
  nothing; started or finished history stays protected as history. A
  later full generation may then replace it like generated work (the
  tombstone names its successor, so the chain of moves stays traceable).
- **Older placements** (origin unknown) are *not* rewritten by any
  migration. `PlanningService.preserved_placement_ids` treats one as
  preserved exactly when its recorded lineage proves it is a move's
  destination -- it superseded a tombstone with `removal_reason =
  "rescheduled"`. Nothing is inferred from coordinates; any other unknown
  placement is replaceable. Releasing such a placement records origin
  `manual`, `preserved = false`.

### Execution cancel reasons

`executions.cancel_reason` says why a `cancelled` attempt was cancelled:
`user` (the lifecycle's cancel action, the default), `rescheduled` (its
placement was moved) or `superseded` (a generation replaced or dropped its
placement before the work started -- a system cancellation, never a user
skip). `null` for any other status, and for cancellations recorded before
Milestone 6 (unknown, not guessed). Cancelled attempts are never reopened,
so a reason never changes.

### Retries and races

- Server and direct writes run under the user's change-log lock; SQLite
  under `BEGIN IMMEDIATE`. Concurrent moves of one placement: exactly one
  wins, the others get `409 deleted`/`version_conflict`
  (`tests/backend/test_postgres.py`, real PostgreSQL).
- **REST is not replayed.** A retry after a lost response is refused like
  any stale write (`409 deleted`). Its `current` is the placement as stored
  now: if its `superseded_by_id` is the `replacement_id` the client chose,
  the move was the client's own. `GET /placements/{id}?include_deleted=true`
  and `GET /executions/{id}` are the reconciliation reads.
- **Sync is replayed** by op_id (section 7): a resent operation answers the
  recorded outcome and writes nothing.

## 4. Placement provenance

New placement fields (local schema v7, server revision 0007, the API, sync
and the canonical CSV):

| Field | Meaning |
| --- | --- |
| `task_category` | The task's category when the placement was saved -- a historical snapshot, like an execution's `category`. Never changes afterwards. `null` for placements saved before it was recorded. |
| `removal_reason` | On a tombstone only: why it left the plan (below). `null` while live, and for tombstones written before reasons were recorded (unknown -- never guessed). |
| `superseded_by_id` | On a tombstone only: the placement that replaced it for the same occurrence, if any. History, not a foreign key. |

| `removal_reason` | Written by | `superseded_by_id` |
| --- | --- | --- |
| `rescheduled` | an explicit reschedule | always the replacement |
| `regenerated` | a generation (`reschedule_range`, `replace_placements`), in or outside the generated range | the new placement of the same occurrence saved by that generation; `null` if the occurrence was not placed again |
| `deleted` | a single placement delete (REST `DELETE /placements/{id}`) | `null` |
| `task_deleted` | the cascade of a task deletion | `null` |
| `reset` | a range reset / clear (and the placements of tasks it deletes) | `null` |

The planned estimate needs no snapshot: it is the placement's own interval
(the engine places exactly the task's estimate, and a move must keep it).

A canonical CSV import keeps the history fields of stored placements when
its rows do not carry them (earlier version 2 files), and never back-fills
them.

## 5. Regeneration (audit)

- An **unchanged** placement keeps its id and version (the engine reuses the
  id of an identical `(task, start, end)`; `_replace_range` keeps the stored
  category snapshot), so no tombstone, supersession or version change is
  written. `workflow.generate` on current inputs writes nothing at all.
- A **changed** placement is a new placement; the old one is a tombstone with
  its original planned values, `removal_reason = "regenerated"` and its
  successor.
- **What every generation keeps** (`workflow.plan_reservations`), on every
  entry point -- the web API (`/planning/generate`), the desktop Day and
  Allocation pages (`PlanningController.generate`), the Week/Month "Make
  Schedule" (`schedule_range`) and the CLI (`generate_day`); there is no
  public way to switch it off (`protect_history` was removed in Milestone 6):
  - *history*: a placement whose execution started or finished stays
    exactly as recorded. If it no longer fits (a fixed block added over it,
    a window change) that is a non-blocking **notice**
    (`GenerationOutcome.notices`, `GenerateOut.notices`); an estimate that
    changed since is never judged against history. New work is placed
    around the free part of its interval;
  - *manual intent*: a preserved placement (above) stays until released;
  - INCREMENTAL also keeps every other saved placement that still fits.
  Kept work is reserved (in memory only) and its occurrence is not placed
  again. With nothing to keep the result is exactly Greedy v1's.
- **Conflicts** (`RegenerationRequiredError`, `409 regenerate_required`): a
  kept manual or incremental placement that no longer fits, or a dependency
  this run would now place after it, is a *blocking* problem; the whole
  generation (every date) writes nothing. Each problem carries
  `placement_id`, `task_id`, `date`, `reason`, `explanation`, `kept_as`
  (`manual` / `kept` / `history` / `destination`), `blocking` and
  `remedies` -- `edit_constraint`, `move`, `release_manual_intent`,
  `choose_another_range`, `regenerate_full`.
- A FULL generation replaces only eligible generated placements. A replaced
  placement's never-started execution is cancelled with `cancel_reason =
  "superseded"` in the same transaction; started or finished executions,
  sessions, snapshots and points are never written by generation.
- **Concurrency.** The engine runs outside any write transaction. The save
  then re-reads every input (fingerprint: tasks, series, exceptions,
  blocks, preferences, external dependencies) and recomputes the
  reservation state -- the generated dates' placements, versions and
  manual intent, execution states, and the occurrences kept outside the
  dates; any difference is `StaleInputsError` (`409 inputs_changed`) and
  nothing is written.

### Scope (Milestone 6)

A generation writes only the dates it generates. An occurrence the range
plans that is already live on another date stays there and is **not**
placed again; it is reported (`GenerationOutcome.kept_elsewhere`,
`GenerateOut.kept_elsewhere`, `RangeScheduleResult.kept_elsewhere`; the
desktop lists it with its date). Moving work across dates is the explicit
reschedule. API change: `superseded_placement_ids` and
`history_protected_placement_ids` of `GenerateOut` (and
`RescheduleResult.superseded_ids` / `history_protected_ids`) are kept for
compatibility and are always empty -- before Milestone 6 a generation
silently removed such placements outside its dates.

## 6. Storage and migrations

**Local SQLite, schema v7** (applied automatically when the app opens the
database; additive):

- `scheduled_tasks.task_category`, `removal_reason`, `superseded_by_id`, with
  CHECKs (the reason is one of the five values; both removal columns are
  `NULL` on live rows; a placement never supersedes itself) and an index on
  `superseded_by_id`;
- the unique index of `executions(scheduled_task_id)` now covers live rows
  only, so a local duplicate that synchronization discarded (a tombstone that
  keeps its sessions) does not block the execution the server kept. Creating
  a new execution for a placement whose execution was deleted is still
  refused (`ExecutionDeletedError`).

Existing rows keep `NULL`; `ALTER TABLE ADD COLUMN` writes no row, so the
upgrade itself marks nothing for synchronization.

**Server, Alembic revision 0007** (`backend/migrations/versions/0007_placement_provenance.py`, additive):

- `placements` and `placement_revisions`: the three columns and their checks;
  `placements.ck_placements_removal_tombstone`; `ix_placements_user_superseded_by`;
- `sync_operation_related_records`: the further record snapshots of an applied
  operation that changed several records.

**Deployment order.**

1. Back up the database, then run `python -m backend.migrate upgrade` (for
   direct mode: `--env-file .env upgrade`). 0007 only adds nullable columns,
   checks that hold for all existing rows, an index and a table, so a server
   of the previous version can keep running during and after it.
2. Deploy the new server.
3. Update desktop clients. A new client needs the new server: its placement
   payloads carry `task_category`, and older servers refuse unknown fields.
   Direct mode refuses to start until the database is at 0007.

**Older clients against the new server** keep working: they never send the
`reschedule` action; placement records gain fields they ignore; their
placement deletes carry no reason, which the server records as unknown
(`null`); they never receive `related` results.

**Milestone 6: local schema v10, server revision 0010**
(`backend/migrations/versions/0010_manual_placements.py`, additive):
`scheduled_tasks`/`placements`/`placement_revisions` gain `origin` (nullable,
CHECK generated/manual) and `preserved` (NOT NULL, default false, CHECK
"only a manual placement is preserved"); `executions`/`execution_revisions`
gain `cancel_reason` (nullable, CHECK "only on a cancelled execution",
user/rescheduled/superseded). Existing rows keep `NULL`/`false`: nothing is
back-filled or guessed, and the upgrade marks nothing for synchronization
(a proven move is recognized from its lineage when read). Deploy order as
above: migrate, then deploy the server, then update clients. A new client
sends these fields only to a server that advertises `manual_placements`
(below); CSV exports gain the optional `origin` and `preserved` columns.

## 7. Synchronization

- **Push.** A local placement tombstone with `removal_reason = "rescheduled"`
  whose server copy (shadow) is still live becomes one operation:
  `{"entity_type": "placement", "entity_id": <moved>, "kind": "action",
  "action": "reschedule", "base_version": <shadow version>, "payload":
  {"replacement_id", "planned_date", "timezone", "planned_start",
  "planned_end", "task_category", "at"}}` (`at` = when the device moved it;
  it becomes the cancelled execution's end time). The server runs the same
  `workflow.reschedule_placement` on its records, under the user's lock, as
  one unit. Offline atomicity comes from this: the move is never sent as
  separate create/delete/cancel operations.
- **Held records.** Until that operation is acknowledged or resolved, the
  replacement (and, for a chain of offline moves, each later replacement)
  and the execution of a moved placement send nothing of their own; a pulled
  change to them only refreshes their shadow.
- **Result.** `{"status": "applied", "record": <the tombstone>, "related":
  [{"entity_type": "placement", "record": <replacement>}, {"entity_type":
  "execution", "record": <cancelled execution>}]}`. Every record is stored as
  an immutable snapshot (`sync_operation_related_records`), so a retry of the
  op_id answers identically and writes nothing (no second placement,
  execution, session, version or change-log entry). The client stores every
  returned record as its shadow; the held records are compared against them
  in the next round (no operation when they match).
- **Refusals** are conflicts carrying the placement as stored now
  (`current`): `deleted` (moved or deleted elsewhere), `version_conflict`,
  `history_protected` (work started elsewhere), `reschedule_rejected` (the
  destination breaks a rule with the server's data). A new operation for
  the same move is refused, not replayed.
- **Resolving.** `accept_remote` stores the server's placement and undoes the
  rest of the local move: the replacement the server never accepted is
  discarded (a local tombstone, never pushed) and the cancelled execution
  returns to its last acknowledged server state (or to `scheduled` if it was
  never synchronized). `keep_local` resends the move against the new
  version (refused for a server tombstone).
- **Deletes** of placements carry `{"removal_reason", "superseded_by_id"}`
  (`rescheduled` is refused there: only the action moves). The successor must
  be one of the user's placements of the same task.
- **Duplicates.** Two devices creating an execution for the same placement:
  the second is refused (`already_exists`); accepting the server's keeps the
  local duplicate as a tombstone. Two devices starting one execution: the
  second is a `version_conflict`; nothing is overwritten.

- **Plans moved before the first sync.** A placement created and then moved or
  regenerated while offline, before the server ever had it, is uploaded as
  history: a `create` carrying its `removal_reason` (`rescheduled` or
  `regenerated`) and `superseded_by_id`, sent after its successor. The server
  stores it as a tombstone at once (`Mutator.create_placement_history`:
  nothing live is created or changed; the successor must be one of the user's
  placements of the same task), and other devices keep such pulled lineage
  tombstones when they have the task. So the original plan and every move
  reach every device.

### Manual placements in synchronization

The server advertises `manual_placements` in `GET /sync/capabilities`. A
client sends `origin`/`preserved` in placement payloads and `cancel_reason`
in execution payloads and cancel actions only to such a server; otherwise
it leaves them out (an older server refuses unknown fields), and a pulled
placement record without them keeps the local origin and intent. A pushed
reschedule makes the server record the destination as manual and preserved
itself, so intent reaches other devices either way. An older client's
placement update keeps the stored origin and intent (omitted = keep).

## 8. Rules of thumb for clients

- Move with `reschedule`; never emulate it with a delete plus a create.
- Choose `replacement_id` yourself when you may need to recognize your own
  move after a lost REST response.
- Read tombstones with `include_deleted=true`; the planned values of a moved
  or regenerated placement are on its tombstone.

## 9. Read contract for analytics (next session)

Everything below is derivable from stored records on SQLite and on the
server alike (`PlanningService.list_placements(include_deleted=True)`,
`placements_superseded_by`, `ExecutionService.list_executions` /
`list_sessions`, or the REST collections with `include_deleted=true`).

**Occurrence chains.** Group placements into chains by following
`superseded_by_id` (a placement's predecessors: `placements_superseded_by`).
A chain is one occurrence of its task (for a series, one materialized
occurrence; for a legacy template placement, one date). Count a chain once; its **head** is the placement nothing supersedes.
Its **original plan** is the first placement of the chain (the tombstone's
planned values and `task_category`); every tombstone keeps its own values, so
each move's before/after is available.

**Classifying a chain (at a reference time `now`)** -- use the head's
execution, if any:

| Head | Head's execution | Class |
| --- | --- | --- |
| live | `completed` | completed (a duration sample) |
| live | `skipped` | skipped by the user |
| live | `cancelled` | cancelled by the user |
| live | `in_progress` / `paused` | in progress |
| live | none or `scheduled`, `planned_end > now` | pending |
| live | none or `scheduled`, `planned_end <= now` | **unattempted** -- never "completed": elapsed planned time proves nothing |
| tombstone, reason `regenerated` / `deleted` / `task_deleted` / `reset` / unknown, no successor | any | removed from the plan (reason as stored); an execution of it keeps its own outcome |

Intermediate links are **superseded**, not missed: an execution on a link
that is `cancelled` while the link's `removal_reason` is `rescheduled` was
closed by the move (exclude it from user cancellations). A `regenerated`
link may still carry an execution (generation never cancels one); treat it
by its own status.

**Duration samples** come only from `completed` executions
(`actual_active_duration_minutes`, sessions excluding paused time). Skipped
or cancelled executions keep sessions for context but are never duration
samples.

**Estimate and category comparisons.** Planned estimate = the placement's
`planned_end - planned_start` (at the time of planning; the execution's
`planned_duration` for executed placements). Planned category = the
placement's `task_category`, else the execution's `category` snapshot; if
both are missing (data older than this milestone without an execution) the
category is **unknown** -- do not substitute the task's current category
without labelling it as such.

**Time.** `created_at`/`updated_at`/`deleted_at` are storage audit times
(server time on the server; an offline move is timestamped when the server
accepts it). The time a move happened on the device is the cancelled
execution's `actual_final_end_at` (when there was one). Planned instants are
aware; use each placement's `timezone` for local dates.

**Never infer:** completion from elapsed time, a category or date for old
rows, or a successor for a tombstone without `superseded_by_id`.

## 10. Remaining limitations

- The desktop moves a placement through its execution actions
  ("Reschedule...") and releases manual intent from the Day timeline
  ("Release manual placement"); other clients use the REST API
  (`/planning/placements/{id}/reschedule` and `/release`) and sync.
- Regeneration does not cancel the `scheduled` execution of a placement it
  replaces (unchanged behavior); analytics classifies it by the tombstone's
  reason (section 9).
- REST has no op_id replay; clients reconcile by reading (section 3).
