# Productivity analytics: metric contract (Milestone 5, session 2)

Two views over the same persisted history, deliberately kept apart:

| View | Entry point | Population | Time basis |
| --- | --- | --- | --- |
| **Terminal outcomes** (existing, unchanged) | `ProductivityService.generate_report` / `build_dashboard` | executions (legacy and canonical), filtered by `created_at` windows | `created_at`; legacy day-index rows included |
| **Schedule cohort** (new) | `ProductivityService.build_schedule_cohort_report`, `GET /planning/analytics/schedule-cohort` | intended occurrences planned in a local date range, with or without an execution | planned instants in an explicit reporting timezone, as of an explicit cutoff |

Both are read-only and deterministic; neither creates executions. There is
no ML in either (the experimental predictor stays inactive).

## 0. Day aggregates of scheduled work (foundation)

`app/productivity/day_summary.py` computes, per date, from the date's live
placements only (tasks the scheduler did not place never count):
`scheduled_count`, `completed_count`, `uncompleted_count`, `pending_count`;
planned minutes of each (`*_minutes`, the placements' own intervals);
`points_scheduled` / `_completed` / `_uncompleted` / `_pending`;
`completed_actual_minutes` and `timed_completed_count` (recorded active time
of completions that were timed -- planned and actual stay separate); and
the classification `status_class`. The outcome is the placement's execution
status (completed; skipped = uncompleted; anything else or none = pending).
Points come from the execution's snapshot when it has one, else from the
task. Clients: the desktop Week/Month pages (`DayOutcomeController`) and
`GET /days/summary`. Answered placements are never moved or replaced by a
re-run (history protection), so past aggregates stay stable.

## 1. Terminal-outcome statistics (compatible)

Unchanged formulas and filters (`app/productivity/stats.py`):

- completion among resolved executions = completed / (completed + skipped + cancelled);
- skip rate among resolved executions = skipped / (completed + skipped + cancelled);
- pending (scheduled, in progress, paused) executions are in neither rate.

Use these labels ("among resolved executions") so they are not confused with
the due-schedule rates below.

## 2. The schedule cohort

Code: `app/productivity/schedule_cohort.py` (pure), read model
`app/planning/history.py`, loaded by the planning repository in one snapshot.

### Parameters

- `start_date`, `end_date`: an inclusive range of local dates (at most 366).
- `timezone`: a validated IANA reporting timezone. The range is the
  half-open instant interval [local midnight of `start_date`, local midnight
  after `end_date`) -- a DST day is 23 or 25 hours long. The host machine's
  timezone is never used: the desktop passes its configured planning
  timezone, the API requires `timezone`.
- `as_of`: an aware cutoff (default: now). A cutoff in the future is
  refused. The report describes the plan and the outcomes **as they were at
  that instant**.

### Occurrences and attribution

- Placements linked by `superseded_by_id` (an explicit move or a
  regeneration that re-placed the occurrence, docs/execution-rescheduling.md)
  are one **lineage** = one intended occurrence, counted once however many
  times it moved.
- Its **applicable placement** is the one live at `as_of` (created at or
  before it, not removed by it). The occurrence belongs to the range when the
  applicable placement's planned start lies in the range; its local date is
  that start's date in the reporting timezone.
- **Moved-out work**: a placement planned in the range but moved or
  re-placed outside it by the cutoff belongs to its new range; it is counted
  here only as `data_quality.moved_out_of_range`. Work moved *into* the range
  counts here, with its full move history.
- **Deletion / reset**: an occurrence whose last placement was removed
  without a successor by the cutoff (`deleted`, `task_deleted`, `reset`,
  `regenerated` with nothing re-placed) is not in any denominator; it is
  counted in `data_quality.removed_from_plan` by reason. Its execution, if
  any, stays in the terminal-outcome view.
- **Unknown history**: tombstones written before placement provenance
  existed (local schema v7 / server 0007) have no reason and no successor:
  they count as `removed_from_plan["unknown"]`, never as reschedules. A
  reschedule is only ever read from `removal_reason = "rescheduled"`.
- Executions that are deleted (tombstoned), not linked to a placement, or
  legacy (no calendar anchor) are not part of the cohort; no date is ever
  fabricated for them.

### State at the cutoff

From the applicable placement's live execution and its recorded instants:

| State | Rule |
| --- | --- |
| completed / skipped / cancelled | terminal, with `actual_final_end_at <= as_of` |
| in_progress | a work session started by `as_of` and still open at `as_of` |
| paused | started by `as_of`, no session open at `as_of`, not terminal by then |
| not_started | no execution, or no work by `as_of` |

A terminal status without a recorded end time is taken as terminal and
counted in `data_quality.terminal_time_unknown`. Nothing is ever completed
because its planned time has passed.

### Due cohort and rates

- **Due** = the applicable planned end is at or before `as_of`. Future
  occurrences are counted separately (`future_count`, `future_states` --
  e.g. work completed early) and never enter a due rate.
- **Due cohort** = due occurrences except explicit (user) cancellations,
  which are reported in `due_outcomes.cancelled`. It includes overdue
  not-started ("unattempted"), in-progress, paused and skipped occurrences.
- **Due completion rate** = completed due occurrences / due cohort.
- **Due skip rate** = skipped due occurrences / due cohort.
- A **late completion** (after the planned end, even on another local date)
  is completed, attributed to its planned date, and counted in
  `start_timing.completed_after_planned_end`.

Every rate is `{numerator, denominator, value, evidence_level,
unavailable_reason}`; a zero denominator gives `value: null` and a reason,
never `0`. `evidence_level` uses the existing thresholds (fewer than 5:
insufficient; 5-14 low; 15-29 moderate; 30+ high).

### Other measures

- **Estimated vs actual**: completed occurrences with a recorded actual active
  duration, paired with the **historical estimate** -- the execution's own
  planned-duration snapshot. Signed error = actual - estimate; ratio =
  actual / estimate over positive estimates only (zero estimates are counted
  in `zero_estimate`); completions without an actual are `missing_actual`.
  Actual active duration is the lifecycle's sum of work sessions, computed
  from aware instants in UTC: pauses, midnight and DST never distort it.
- **Start timing**: signed start delay = first work start - applicable
  planned start (UTC arithmetic; negative = early); lateness = max(0,
  delay). Occurrences not started by the cutoff are `unknown`, never 0.
- **Reschedule rate** = distinct occurrences of the range explicitly moved at
  least once (by the cutoff) / all occurrences of the range (due and future,
  any state). `reschedule_events` counts moves separately (so the rate can
  never exceed 100%); regeneration replacements are counted apart
  (`regenerated_occurrences`, `regeneration_events`).
- **By category**: the historical category -- the placement's
  `task_category` snapshot, else the execution's `category` snapshot, else
  `"unknown"` (the task's current category is never substituted). Same
  due-cohort definitions per slice.
- **By planned time of day**: the applicable planned start's local time in
  the plan's own timezone (night 22-06, morning 06-12, afternoon 12-18,
  evening 18-22). **By actual start time of day** is a separate view over due
  occurrences that started.
- **Workload minutes**, each with its basis: `due_scheduled_minutes`
  (planned intervals of the due cohort), `future_scheduled_minutes`,
  `completed_planned_minutes` (planned intervals of completed due
  occurrences), `completed_actual_active_minutes` (their recorded actual
  minutes; `completed_missing_actual` lacks one).
- **Underestimation**: groups are a historical category or one task identity
  (`task_id`) -- never a display name. A group is *consistently
  underestimated* when it has at least 5 paired completions, at least 70% of
  them ran longer than estimated, and the median actual/estimate ratio is at
  least 1.1. Smaller groups are shown with `evidence: "insufficient: n of 5"`
  and never flagged.
- **Day signals** (per local date of due work): unfinished planned minutes
  (due, not completed, not cancelled) and their share. The
  `high_unfinished_workload` signal needs at least 120 unfinished minutes
  *and* at least 50% unfinished; it carries its reasons. It is a signal, not
  proof of a cause. **Capacity is unknown**: the day window in effect on a
  past date is not recorded, and today's preferences are not used to
  reconstruct it (`available_minutes: null` with `capacity_note`).
- **Data quality**: `category_unknown`, `terminal_time_unknown`,
  `completed_missing_actual`, `zero_estimates`, `removed_from_plan`,
  `moved_out_of_range`.

## 3. Contracts

**Service.** `ProductivityService(repository, thresholds, clock, *, history,
timezone_name)`. `history` is the owner-scoped `PlanningService` (anything
with `schedule_history(start_utc, end_utc)`), `timezone_name` the reporting
timezone. The desktop builds it from `AppServices.timezone` (SQLite) and the
direct PostgreSQL composition passes its configured timezone.

```python
report = productivity.build_schedule_cohort_report(start_date=date(2026, 3, 2), end_date=date(2026, 3, 8),
                                                   timezone_name=None,  # the service's reporting timezone
                                                   as_of=None)          # now
```

`ProductivityController.build_schedule_cohort_report(start_date, end_date,
*, timezone_name=None, as_of=None)` wraps it in a `ControllerResult`.

**Read snapshot.** `PlanningService.schedule_history(start_utc, end_utc)`
reads, in the service's owner scope, the placements (tombstones included)
planned to start in the range, every placement they superseded, and the live
executions (with sessions) of the in-range placements: on SQLite in one
transaction; on the server/direct path in one session after reading the
user's change-log row `FOR SHARE` (every writer of that user holds it
exclusively), so no write of the user can commit in between. Queries are
bounded by the range and by the lineage depth (64).

**HTTP.** `GET /planning/analytics/schedule-cohort?start_date=&end_date=&timezone=&as_of=`
on the hosted server and on the local profile (the same planning router),
authenticated and scoped to the caller; the response is the
`ScheduleCohortReport` JSON. `422 validation_error` for an invalid range,
timezone or a future/naive cutoff; `401` without credentials.

**Sample** (from `tests/direct/test_schedule_cohort_adapters.py`, trimmed):

```json
{"timezone": "America/Vancouver", "start_date": "2026-03-02", "end_date": "2026-03-04",
 "as_of": "2026-03-03T12:00:00Z", "occurrence_count": 7, "due_count": 6, "future_count": 1,
 "due_outcomes": {"completed": 1, "skipped": 1, "in_progress": 1, "paused": 0,
                  "overdue_unattempted": 2, "cancelled": 1},
 "due_completion": {"numerator": 1, "denominator": 5, "value": 0.2, "evidence_level": "low"},
 "due_skip": {"numerator": 1, "denominator": 5, "value": 0.2, "evidence_level": "low"},
 "reschedules": {"reschedule_rate": {"numerator": 1, "denominator": 7, "value": 0.1429, "evidence_level": "low"},
                 "reschedule_events": 1, "regenerated_occurrences": 0, "regeneration_events": 0},
 "duration": {"pairs": 1, "total_estimated_minutes": 60.0, "total_actual_active_minutes": 40.0,
              "median_signed_error_minutes": -20.0, "median_ratio": 0.667},
 "day_signals": [{"local_date": "2026-03-02", "unfinished_planned_minutes": 240.0, "unfinished_share": 0.8,
                  "high_unfinished_workload": true, "available_minutes": null}]}
```

## 4. Historical-data limitations

- Placement categories are known from Milestone 5 on; older placements
  without an execution have an unknown category.
- Removal reasons and successors are known from Milestone 5 on; older
  tombstones are "removed, reason unknown" and never counted as moves.
- Past day windows are not recorded, so historical capacity is unknown.
- Execution deletions apply to every cutoff (a deleted execution is out of
  every report, even for a cutoff before its deletion).
- Placement creation times on the server are the server's acceptance times;
  for a cutoff inside a device's offline period, a placement created offline
  appears only from when it reached the server.
- Legacy (day-index) executions have no calendar anchor and appear only in
  the terminal-outcome view.

## 5. The tracker (awards, averages, task types and time views)

`ProductivityService.build_tracker_report(range_days=None, filters=None)`
(`app/productivity/tracker.py`) feeds the desktop Productivity page's three
sections: General, Task-based and Time-based. It is read-only and recomputed
from the stored records every time. There is no stored counter and no award
ledger, so a reopened or deleted completion stops counting and a
re-completion counts once.

### Date bases

Three bases are kept apart and are named wherever a figure is shown.

| Basis | Used for | A record belongs to |
| --- | --- | --- |
| Execution created | The terminal-outcome statistics of sections 1 (unchanged) | the execution's `created_at` |
| Planned date | Status counts, due rates, day classes, streaks, type counts, durations | the local date of the applicable planned start (one placement lineage = one occurrence, counted once) |
| Completion date | Earned points, completed activity, productive minutes, point awards | the local date of `actual_final_end_at`, whatever the planned date and whether or not the plan or task still exists |

Dates are local dates in the explicit reporting timezone. Weeks are
Monday–Sunday. Planned-start buckets (night 22:00–05:59, morning
06:00–11:59, afternoon 12:00–17:59, evening 18:00–21:59) use each plan's own
timezone, as the cohort does.

### Formulas

- **Due completion** = due completed / (due completed + skipped + not started + in progress + paused).
  Cancelled, future and removed occurrences are excluded and shown separately.
  **Due skip** uses the same denominator. A zero denominator is unavailable, never 0%.
- **Completion among resolved** (execution-based, unchanged) = completed / (completed + skipped + cancelled).
  `SegmentStats.completed_count`, `skipped_count` and `cancelled_count` are the actual counts.
- **Unresolved** = not started + in progress + paused. **Overdue, not started** is its due, not-started part.
- **Points** are the execution's `points` snapshot, never a placement's optimizer `score` and never the
  task's current value. A completion without a snapshot has unknown points; it is counted, its points are not.
- **Highest-point day**: the largest sum of known points over finished local dates. Today is returned as the
  partial period and cannot be the record.
- **Best week**: the largest sum of known points over whole Monday–Sunday weeks that lie between the first
  activity and yesterday. The current week is the partial period.
- **Most completed type**: the type with the most completions in the range; its due-completion rate is shown
  beside it and does not decide the ranking.
- **Green-day streaks**: a day is green when `classify_day` says at least 60% of its scheduled occurrences are
  completed (both green classes; a cancelled attempt counts as uncompleted there). An empty day and a finished
  non-green day end a streak. Today is provisional while it has no occurrences or any unresolved one. The
  longest streak is searched in the selected range; the current streak uses the whole history.
- **Averages**: per elapsed calendar day from the first planned-or-completed activity in the range through
  yesterday, zero-work days included; per complete Monday–Sunday week (unavailable when there is none).
  The average daily due completion is the unweighted mean over days with due work; the pooled rate is separate.
- **Highest-completion weekday**: the highest due-completion rate among weekdays with at least
  `ProductivityThresholds.low` (5) due occurrences. **Highest-points weekday**: known points divided by the
  elapsed calendar dates of that weekday (dates without points count as zero).
- **Most-supported slot**: the planned-start bucket with the most timed completions. It says where the evidence
  is, not where performance is best.

Every tie is returned (earliest or alphabetically first is the representative). Evidence levels use
`ProductivityThresholds` (5 / 15 / 30) on the metric's own denominator.

### Task types

A task type (`TaskType`, `Task.task_type_id`) is an identity separate from category, tags and occurrences. A new
task gets a type derived from its own id unless the user picks an existing type or names a new one in the task
form. Every occurrence of a recurring series, and every later segment of the series, shares the series' type.
Tasks are never grouped by name. Category and tag filters select records by their snapshots and never regroup
types. Items whose type is unknown are grouped apart under "Unknown type".

Schema v12 (desktop) and revision 0013 (server) gave every existing task its derived type. Existing placements
keep an unknown planning snapshot.

### Completeness and historical gaps

- The whole history is read in windows of at most 366 days, merged as raw records, so medians are exact and a
  streak can cross a window. Reading stops 30 × 366 days back; older history sets `history_truncated`.
- A chain of moves longer than 64 steps sets `lineage_truncated`.
- Completions without `actual_final_end_at` cannot be dated and are counted in `unknown_completion_dates`.
- Placements saved before planning snapshots have no recorded name, tags or points; their name falls back to the
  execution's snapshot when there is one, and their type to the task's current type.
- Any of these marks the report incomplete and qualifies the awards it could change.

### Storage profiles and deletion

- **This device** (ownerless local workspace), **synchronized account** (local database plus HTTP sync) and
  **direct server** (PostgreSQL, no offline replica) all run the same service over their own repository and give
  the same report for the same records.
- Deleting execution history removes its completions, points and timed metrics from every figure above. Plans
  are not deleted, so due plans become overdue and not started. In a synchronized account the deletion reaches
  the server and the other devices and is not resurrected by a stale device.
- Deleting a task or removing a placement does not delete a recorded completion: it still counts on its
  completion date, with the name, type and points recorded at the time.
- Exports (CSV / JSON) contain the whole workspace's execution history regardless of the page's filters.
