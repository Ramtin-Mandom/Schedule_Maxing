# Recurring series and occurrences

A repeating task is a **series definition**: a `Task` whose `recurrence` is set. It is never scheduled
itself. Each original recurrence date (a **slot**) is materialized as its own concrete `Task` -- an
**occurrence** -- that is allocated, placed, executed, moved, edited and deleted like any other task.

| Module | Role |
|---|---|
| `app/planning/recurrence.py` | The calendar (slots, counts, bounded seeking), occurrence ids, cadence compatibility, DST detection. Pure. |
| `app/planning/series.py` | Expansion and scoped edits/deletes, on any `PlanningService` (SQLite or the server's). |
| `app/planning/occurrence.py` | Occurrence identity of placements (supersession, protection, moves). |
| `app/planning/models.py` | `RecurrenceSpec.start_date/timezone`, `Task.series_id/occurrence_slot/occurrence_state/series_version/series_predecessor_id`, `OccurrenceState`. |

## Configuration

A series is **configured** when its rule names an explicit local start date (the anchor) and an IANA time
zone (`RecurrenceSpec.start_date` and `.timezone`, set together). Neither is ever derived from a requested
range, a placement or the machine clock.

Templates stored before this change have no anchor and no zone. No stored record can prove both (tasks never
stored a zone), so every such template **needs configuration**: it produces no slots, Week/Month list it as
"needs setup", and the task form says so. An ordinary edit of it (a rename) changes only that; choosing
"Repeats" in the form configures it, starting on the date shown in the page's time zone.

A configured series is dated by its rule: it has no `required_date`, `preferred_dates` or `deadline`.

## Calendar semantics

Slots are local calendar dates in the series' time zone.

- **daily**: every `interval` days from the anchor.
- **weekly**: Monday-anchored weeks, every `interval` weeks counted from the anchor's week; in each such week
  the selected weekdays, or the anchor's weekday when none are selected.
- **monthly**: every `interval` months counted from the anchor's month, on `day_of_month` or the anchor's
  day. A month without that day has no slot: it is **skipped, never clamped** (no 31st in April; February 29
  only in leap years, so not in 2100).
- No slot precedes the anchor (e.g. selected weekdays of the anchor's week before the anchor).
- `end_date` is inclusive. An end date before the start is a *retired* segment with no slots.
- `count` counts valid slots from the anchor, independent of any requested window. A skipped, deleted or
  moved occurrence still consumes its slot. `count` and `end_date` remain mutually exclusive.

Slot indexes and the first slot of a window are computed arithmetically (monthly day-of-month validity uses
the 400-year Gregorian cycle), never by stepping day by day from the anchor; every enumeration is capped by
`MAX_SLOT_SCAN` steps. Randomized tests compare the arithmetic with brute force.

## Identity

`occurrence_task_id(series_id, slot) = uuid5(namespace, "<series id>/<slot ISO date>")`. The mutable
placement date, the series' definition version and the device are not inputs, so two devices expanding the
same rule mint the same ids. An occurrence's `series_id` and `occurrence_slot` never change (the model
rejects an id that does not match them; updates, imports and the server refuse changes).

Both stores enforce one record per (series, slot) -- tombstones included, so a suppressed slot stays
reserved: SQLite `idx_tasks_occurrence_slot (series_id, occurrence_slot)`, PostgreSQL/SQLite server
`uq_tasks_user_series_slot (user_id, series_id, occurrence_slot)`. An occurrence belongs to its series'
owner; the server's composite foreign key `(user_id, series_id) -> tasks` makes another user's series
unreachable.

`series_version` records the series version an occurrence was materialized (or last refreshed) from. It is
provenance, never identity, and not a scheduling input.

## Expansion

`series.expand_occurrences(service, start, end)` materializes the slots of `[start, end]` for every live
configured series in the service's scope:

- bounded by the planning-range limit (62 days, `ScopeError`) and `MAX_EXPANSION_OCCURRENCES` created rows
  per call (`RecurrenceLimitError`); nothing is written unless all of it fits and validates, then all of it
  is written in one transaction;
- idempotent: repeated, overlapping, restarted and retried expansion finds existing records (live or
  tombstoned) and does nothing -- no version bump, no change capture;
- concurrent expansion on two connections or devices converges on one record per slot;
- a slot whose date changes UTC offset in the series' zone, or whose preferred window starts/ends at a
  nonexistent or ambiguous local time, is reported as a warning. Times are never shifted; scheduling a window
  that crosses an offset change stays refused by `app/planning/time.py`;
- `dry_run=True` reports without writing.

Every persisted generation path expands its allocation range first: `workflow.generate` and
`workflow.preview_allocation` (so a preview and the generation it confirms read the same inputs),
`PlanningController.schedule_range` / `allocate_range` (the desktop Make Schedule and allocation, which run
in background workers), and the HTTP generation endpoints (hosted and local).

`load_range` never returns a series definition as a task to schedule. It returns the range's series
definitions separately (`PlanningRange.series`) because they are inputs: the inputs fingerprint includes
them, and leaves unset recurrence fields out of every task's content, so schedules saved before this change
stay current after the upgrade.

## Exceptions and scoped changes

An occurrence's `occurrence_state` records its exception:

| State | Record | Meaning |
|---|---|---|
| none | live | follows its series |
| `modified` | live (or a later tombstone) | edited on its own; series-wide edits leave it alone |
| `skipped`, `deleted` | tombstone | removed by the user; the slot stays reserved |
| `superseded` | tombstone | removed by a "this and later" or series-wide change |

Operations (`app/planning/series.py`, each one atomic and versioned -- the target's expected version is the
precondition, every other record is read and written in the same transaction):

- **This occurrence** -- `edit_occurrence` (any content, including its date: moving keeps its slot
  identity; it becomes `modified`); `delete_occurrence(skip=...)`.
- **This and every later occurrence** (cutoff = the occurrence's original slot) -- `edit_series(scope=FUTURE)`
  splits: the segment ends the day before the cutoff (a count becomes the equivalent end date) and a new
  segment with the new definition starts at the cutoff, linked by `series_predecessor_id`. A count keeps
  meaning "from the original start" when the cadence is unchanged (the successor gets the remainder).
  `delete_series(scope=FUTURE)` only ends the segment.
- **Entire series** -- `edit_series(scope=SERIES)` updates the definition in place when the rule is unchanged
  and refreshes every occurrence still following it; a rule change (cadence, bounds, start, **time zone**) is
  a versioned split at the segment's start, so historical occurrences keep their ids and slots.
  `delete_series(scope=SERIES)` tombstones the definition.

Occurrences from the cutoff on are superseded, **except** preserved ones -- an occurrence whose execution has
started or finished (in progress, paused, completed, skipped, cancelled) or a `modified` one. Results name
every preserved record and why (`SeriesChange.preserved`, `.explanation()`). A preserved occurrence keeps
covering its date: the successor segment never mints that date again (no duplicate slot), while superseded
dates are materialized again under the successor. Segments never overlap. Execution records are never
touched; nothing is hard-deleted. "Entire series" is the segment the target belongs to.

Through the generic task API, an update that changes an occurrence's content marks it `modified`, and deleting
an occurrence records `deleted`.

## Moving occurrences and legacy placements

`occurrence_key` is `(series_id, occurrence_slot)` for an occurrence, `(task_id, planned_date)` for a
placement of a series definition (only *legacy* placements, saved before expansion) and `(task_id, None)`
otherwise.

- Moving an occurrence's placement to another date (`workflow.reschedule_placement`) also re-dates the
  occurrence (`required_date`, `modified`) in the same transaction (`PlacementReschedule.updated_task`): the
  date it left can never produce its work again, and its new date keeps it once. A second live placement of
  the same occurrence is refused (`occurrence_taken`).
- Legacy placements keep their ids, task ids and the executions recorded against them -- nothing is
  rewritten. Expanding a range maps them by identity: a date with exactly one live legacy placement of a
  template gets that slot's occurrence, whose placement then supersedes the legacy one (lineage via
  `superseded_by_id`) instead of duplicating it; started/finished legacy work keeps the slot (the occurrence
  is not placed again). A date with several live legacy placements is an **ambiguous collision**: reported
  (`ExpansionResult.legacy_collisions`, problem `legacy_collision`) and left untouched for repair. A legacy
  placement still moves only within its own date.

## Dependencies

- An ordinary dependency names the concrete task (an occurrence included).
- A **series depending on a series** resolves, per slot, to the prerequisite lineage's occurrence of the *same
  original slot* (never every occurrence, never "the latest"). Supported: the same time zone and either a
  daily-every-day prerequisite or the same frequency and interval in phase (weekly: the dependent's weekdays
  among the prerequisite's; monthly: the same day). Mismatches are refused when the dependency is saved.
  Expansion materializes the needed prerequisite occurrences (within the same budget, bounded lineage walks).
  A prerequisite without that slot (it starts later, ended, repeats on other dates) leaves the dependent's slot
  unmaterialized with a `dependency_unresolved` problem.
- A one-off task or an occurrence cannot depend on a series definition: it names one concrete occurrence.
  Edges stored before these rules are kept; external-dependency resolution reports such a series dependency
  as `series` (blocking, with an explanation).
- Series cycles are refused. Skipping a prerequisite occurrence is allowed although a dependent occurrence
  keeps its same-slot edge to it; the dependent then reports the prerequisite as missing.

## Storage and migrations

- SQLite schema **v9** (`app/execution/db.py`): the seven task columns, CHECKs (anchor and zone together,
  identity only on occurrences, tombstone states only on tombstones), the unique slot index, lineage and
  series indexes, and a deferred foreign key to the series row. Additive: every existing row gets NULL; no
  change capture.
- Server revision **0009** (`backend/migrations/versions/0009_recurrence_occurrences.py`): the same columns on
  `tasks` and `task_revisions`, CHECKs, `fk_tasks_series`, `uq_tasks_user_series_slot` and a lineage index.
  Expand-only; `downgrade()` (disposable databases) drops them.
- A failing SQLite v9 leaves v8 intact; a failing 0009 leaves 0008 (both tested).

## Synchronization and older clients

- Occurrences travel as tasks with their recurrence fields; series ids order before occurrences, and a
  predecessor before its successor.
- All pending task operations of one lineage (updates, creates, moves of its occurrences, deletes of
  superseded occurrences and their placements) are pushed as **one atomic group**, so a split never
  half-applies; a lineage with more than `_MAX_GROUP_OPERATIONS` (150) operations is sent ungrouped.
- A server create that repeats a stored occurrence with the same content is answered `applied` with the stored
  record (no new version or change-log entry); a different one is an `already_exists` conflict -- never a
  second occurrence. A pulled occurrence equal to this device's pending copy converges without a conflict.
- An occurrence tombstoned before it ever synced is pushed as a tombstone create; a pulled occurrence tombstone
  is stored even if never seen here. Either way the slot stays reserved on every device.
- Task deletes carry `{"occurrence_state"}`; a cross-date reschedule returns the re-dated occurrence among its
  `related` records.
- **Capability negotiation**: `GET /sync/capabilities` returns `protocol_version` (2) and `features`
  (`recurrence_occurrences`). A client sends recurrence data only to a server listing that feature; for an older
  server (404) it *holds* those records -- configured series, occurrences, lineage, and the placements and
  executions of occurrences -- and reports `SyncReport.held`, instead of letting their fields be dropped.
- An **older client** that omits the new fields cannot erase them: on update the server keeps every omitted
  recurrence field (and a rule's anchor), and an older client's content edit of an occurrence marks it
  `modified`. Older clients still see series definitions as plain tasks; that limitation is theirs.

Conflict recovery for these records follows the existing conflict UI; Prompt 3 hardens it.

## CSV

The canonical planning CSV (format version 2) appends optional columns `series_id`, `occurrence_slot`,
`occurrence_state`, `series_version`, `series_predecessor_id`; a series' anchor travels inside its
`recurrence` JSON. A complete export (`include_deleted`) round-trips series, occurrences, exception states,
lineage and reserved-slot tombstones; an older file imports its templates as needing configuration. A file
cannot change an occurrence's identity (the derived id no longer matches; a batch update is refused).

## Desktop

The reusable task form has **Repeats** (daily / weekly with weekdays / monthly with a day of month, every N,
ending never / on a date / after N times), starting on the page's date in the page's zone (an edited series
keeps its own). Week and Month list each series that can repeat in the shown dates ("repeats", with a
description) and every template that needs setup. Saving an edited occurrence asks: only this occurrence /
this and every later occurrence / every occurrence; removing one offers skip / delete this occurrence / this
and later / the entire series (`ChoiceDialog`). Storage calls go through the pages' existing `_io` path, and
expansion runs inside Make Schedule and allocation, which already run in background workers; a result started
before an account switch is dropped by the workspace guard.

## API for the next prompts

Service level (`app/planning/series.py`, on any scoped `PlanningService`):

```python
expand_occurrences(service, start, end, *, series_ids=None, budget=MAX_EXPANSION_OCCURRENCES,
                   dry_run=False, clock=None) -> ExpansionResult
    # .created, .existing_count, .needs_configuration, .problems, .warnings, .legacy_collisions
edit_occurrence(service, edited_occurrence, *, expected_version) -> SeriesChange
delete_occurrence(service, occurrence_id, *, expected_version, skip=False) -> SeriesChange
edit_series(service, definition, *, expected_version, scope=EditScope.SERIES|FUTURE, cutoff=None) -> SeriesChange
delete_series(service, series_id, *, expected_version, scope=EditScope.SERIES|FUTURE, cutoff=None) -> SeriesChange
    # SeriesChange: .series, .successor, .occurrence, .updated, .superseded, .preserved (task, reason), .problems
series_of(service, task) -> Task | None
```

`PlanningService`: `list_series`, `occurrences_of_series`, `occurrences_in_slot_range`, `series_successors`,
`execution_statuses_for_tasks`, `materialize_occurrences`, `refresh_occurrences`, `delete_occurrences(state=)`.
`workflow.expand_range` expands an allocation range; `GenerationOutcome.expansion` / `AllocationPreview.expansion`
carry what ran. Occurrence identity for placements: `occurrence_key`, `task_occurrence_key`,
`HISTORY_PROTECTED_STATUSES` (`app/planning/occurrence.py`).

HTTP (hosted and local profiles): `POST /planning/recurrence/expand`, `POST /planning/occurrences/{id}/edit`,
`POST /planning/occurrences/{id}/delete`, `POST /planning/series/{id}/edit`, `POST /planning/series/{id}/delete`;
desktop: `PlanningController.expand_range / edit_occurrence / edit_series / delete_occurrence / delete_series /
list_series`, `SchedulePageController.scope_choices / removal_choices / save_draft(scope=) / delete(scope=)`.

## Limitations

- Occurrences are materialized per range (when a range is generated, previewed or expanded), not for all time.
- "Entire series" means the current segment; earlier segments split off by "this and later" changes keep their
  own history.
- A superseded slot stays reserved; if a later rule change produces that date again, the new segment
  materializes it under its own id.
- Manual-placement intent and regeneration policy for moved occurrences are Prompt 2; conflict-recovery
  hardening is Prompt 3.
