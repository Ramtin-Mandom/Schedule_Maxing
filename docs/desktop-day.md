# The Day Schedule (Milestone 4, Prompt 4)

The Day page is the desktop's main workspace for one date. It schedules
locally, through the shared workflow (`app/planning/workflow.py`). There is no
HTTP call and no local server.

| Layer | Module |
| --- | --- |
| Page (widgets) | `app/ui/day_page.py` (`DaySchedulePage`) |
| Timeline widget | `app/ui/day_timeline.py` (`DayTimeline`, `TimelineGeometry`) |
| Presenter (Tk-free) | `app/ui/day_controller.py` (`DayScheduleController`, `DaySnapshot`, `DayRun`) |
| Preference fields (Tk-free) and editor | `app/ui/preferences_model.py`, `app/ui/preferences_editor.py` |
| Shared task actions | `app/ui/task_actions.py` (Day, Week and Month), `app/ui/task_list.py` |

## Opening a date

- The Day page is **today**: the app opens on today's real date, and
  choosing Day Schedule in the navigation (or Ctrl+1) returns to today. Today
  comes from the computer's clock in the planning timezone, which is the
  computer's own time zone (`app/planning/system_clock.py`; override with
  `SCHEDULE_MAXING_TIMEZONE`). A Vancouver user at 11 PM sees that evening's
  date, not UTC's next day.
- Saved data is loaded at once.
- The task form has no date field: a task or fixed block added here belongs to
  the date shown.
- Navigate with **‹ Previous day**, the date field (**Go** or Enter),
  **Today** and **Next day ›**.
- A date opened from Week or Month (**Open Day**, or the task list's **Open
  date in Day Schedule**) shows **‹ Back to Week (…)**. It returns to that page
  with its own dates. The way back is kept while the Day page is rebuilt, for
  example after an account switch.

## The timeline

- A horizontal strip of the whole day, with hour markers (12 AM … 11 PM).
  One logical pixel is one minute, so every block starts at its own minute
  and is exactly as wide as it lasts.
- **Fixed blocks** appear as soon as they are saved. They use their stored
  category's color; an imported category gets its own stable color and is
  never renamed.
- **Scheduled tasks** appear after Make Schedule. A schedule that is out of
  date has a dashed warning outline and says "Out of date".
- The part of the day outside the effective **day window** is shaded.

## The Day Window

Above the timeline (and above the Week/Month calendars, for the selected day)
the **Day Window** bar shows the date's start and end of the usable day in the
shared [hour]:[minute] [AM/PM] input (`app/ui/day_window_bar.py`,
`app/ui/day_window.py`):

- Every date starts with the **default** from Settings (Default scheduling
  preferences → Day window); the badge says **Default**.
- **Apply to this date** saves an override for that date only (**Custom for
  this date**). It is the `day_window` field of the date's own preference
  layer -- the same record that is stored locally, synchronized to the
  server's PostgreSQL, or written directly with direct storage -- and every
  other field of that layer (the engine, category values) is kept.
- Changing the default in Settings moves every date without an override;
  customised dates keep theirs. **Use default** removes the override.
- Refused, with nothing saved: an end not after the start, an unreadable time,
  a time that does not exist on that date (daylight saving), or a window that
  would leave one of the date's fixed blocks outside it.
- Make Schedule places flexible work only inside the date's effective window.
  Saved work that no longer fits makes the schedule out of date; Make Schedule
  then reports it instead of moving anything silently.
  **Free time** inside the window is drawn with a dashed outline. Free time is
  calculated for display only and is never saved as a task.
- Items that overlap (possible only for out-of-date work) get their own row.
- **Keyboard:** Tab into the strip. Left/Right/Home/End select an item. Enter
  (or a double-click) edits it, Delete removes it, and the Menu key or
  Shift+F10 opens its actions.
- The selected item's details are written out below the strip, with h:mm
  AM/PM times, duration, kind and category. With nothing selected, the line
  lists the window and the free time.
- The Available tasks buttons open unscheduled tasks for editing; scheduled tasks
  are marked done or not done on the Uncompleted | Tasks | Completed board below.

## Available tasks

- Lists the date's tasks, and undated tasks, that are not on the schedule.
  An undated task already scheduled on another date is left out.
- Each entry is a button that opens the task for editing.
- A reason is shown only when it is actually known: it comes from a run made
  in this session, while the saved schedule is still that run's.
- After a restart only the saved *count* of unplaced tasks is known. The page
  says so and does not make up reasons.

## Engine

The **Engine** choice sits right beside **Make Schedule**:

| Label | Engine | In practice |
| --- | --- | --- |
| Normal | `precise_greedy` | Tasks may start at any minute |
| ADHD friendly | `adhd_friendly` | Tasks longer than 30 minutes start on the quarter hour; shorter tasks still start at any minute; durations never change |

- The choices come from `OptimizerMode` and the service's engine catalog.
  Their labels live in one place, `app.planning.preferences.ENGINE_LABELS`,
  so a future mode needs one label and one explanation.
- Normal is only the fallback when no saved preference chooses an engine.
  Opening the app writes nothing, so an existing preference is never
  overwritten.
- The note under the choice shows the date's effective engine and whether it
  is set for this date or is the default.
- Choosing an engine saves only **this date's** `optimizer_mode` override
  (`PlanningController.update_date_overrides`). Every other field of the date
  layer, and your defaults, are kept. **Use default engine** removes just
  that field; a date layer left empty is deleted.
- The save runs in the background. Make Schedule, the choice itself and the
  other data actions are disabled until it finishes.
- If the save fails, the choice snaps back to the saved engine and nothing is
  generated with it.
- A change marks the saved schedule **Out of date** and says that the engine
  changed. It does **not** regenerate by itself.

## Make Schedule

| Saved state of the date | What Make Schedule does |
| --- | --- |
| Nothing saved | A full generation of the date |
| Current | Nothing. It reports "already current"; no record, version, timestamp or sync change |
| Out of date, with saved work | An **incremental** run: every placement that still fits keeps its id, times and execution links, and only new work is placed around it |
| Kept work no longer fits (edited task, new fixed block, a quarter-hour rule after switching to ADHD friendly, …) | Nothing changes. It lists which work no longer fits and why, and offers **Regenerate** |

- **Regenerate…** asks first. It is a full generation that protects history:
  work that has started or finished stays where it is and is never
  duplicated.
- Every run saves atomically: placements and provenance in one transaction,
  after re-checking that its inputs did not change meanwhile.
- These outcomes keep the previous schedule exactly as it was:
  - a failure (a required task that cannot be placed, inputs that changed
    during the run);
  - a run that would place nothing while saved work exists: nothing
    allocated to the date ("no work"), or nothing fits ("no capacity"). This
    uses the workflow's opt-in `preserve_on_empty`.
- Required-task failures are listed first, marked "Required:". Optional tasks
  that could not be placed follow, each with its engine or allocation reason.
- Runs happen in a background worker. A result that arrives after the date
  changed is not shown as this date's result (the page re-reads instead). A
  result from before an account switch is dropped
  (`AppServices.workspace_guard`).

## Freshness

The header badge reads **Current**, **Out of date** or **Not scheduled
yet**. It comes from the persisted provenance and the shared
`workflow.day_freshness`, so it is correct after a reload, a restart, a sync,
and any task, fixed-block, preference, allocation or engine change.

An out-of-date schedule says why:

- the inputs it was made from changed;
- its saved entries changed;
- it was saved before schedules were tracked;
- or the engine changed from one to the other.

## Day Preferences

- A native dialog, not a YAML editor.
- It shows every field the current engine actually reads:
  - the scheduling window;
  - priority, preferred-time and related-tag weights and distances;
  - the fragmentation penalty;
  - the preferred spacing (a **soft** preference, not a guaranteed break);
  - per-category importance and preferred time;
  - with ADHD friendly only, the short-gap bonus.
- The inactive `weight_category_bonus` and the legacy Greedy Optimizer v1 /
  annealing settings are not shown.
- Each row shows the value scheduling uses and where it comes from: set for
  this date, inherited from your defaults, the app default, or cleared.
- **Save** sets the value for this date. **Use inherited** removes it (the
  field is absent from the layer). For category fields, **No preference**
  stores an explicit null, which cancels a lower layer's value for this date.
- **Reset this date…** removes the whole date layer, engine included.
- Values are checked the way scheduling resolves them before anything is
  saved. A bad value is shown next to its field.
- Every save passes the version it read. A change made elsewhere is refused
  and the dialog shows the latest values.
- The controls (`PreferencesEditor`, `preferences_model.field_specs/
  preference_rows/with_value/inherit/clear`) are meant for the later default
  Settings page, which will edit the user layer.

## Reset Day

1. **Reset Day…** first previews the reset (`reset_preview`) and describes
   it:
   - the date's tasks, fixed blocks, scheduled entries and schedule record;
   - the date's own preferences and engine;
   - entries of those tasks on other dates that go with them;
   - repeating tasks that are kept;
   - execution history, which is kept.
2. Cancelling deletes nothing.
3. Confirming applies exactly the previewed records (the preview's token).
   If anything changed since the preview, the reset is refused and nothing is
   deleted.

- A reset whose tasks are needed by tasks on other dates is refused, naming
  them.
- Afterwards the form is cleared.
- Undated tasks, tasks on other dates, projects, your default preferences
  (no weight is zeroed) and all execution history are kept.

## CSV

- **Export CSV…** writes the date's records in the canonical format
  version 2. Ids, owners, recurrence and metadata are kept.
- **Import CSV…** reads a file and **previews** it without writing:
  - what would be created, updated, deleted or left unchanged;
  - or every problem: duplicate ids, bad rows, dates and times, broken
    references, an unsupported format version, a version conflict or another
    owner.
- After confirmation it is applied in one transaction, with version and
  ownership checks. Records that differ from what is saved are updated only
  when their version still matches.
- A legacy half-hour schedule CSV is never imported silently. The page names
  it as legacy, and imports it only after an explicit **Append** or
  **Replace** choice and a confirmation.
- Other pages re-read their data when shown.

## Uncompleted | Tasks | Completed

Below the actions, one board of three rounded columns replaces the former
"Tasks on this date" list and Execute tab (`app/ui/task_status_board.py`,
Tk-free model `app/ui/task_status.py`). It holds **only tasks the saved
schedule placed on the date** -- its live placements. A task the scheduler
could not place was never scheduled: it stays in **Available tasks** with its
reason and never appears in any column.

| Column | The placement's execution |
| --- | --- |
| **Tasks** | none yet, `scheduled`, `in_progress` or `paused` |
| **Completed** | `completed` |
| **Uncompleted** | `skipped` (a `cancelled` attempt is shown here too, and cannot be moved) |

Controls: in Tasks, **×** marks a task uncompleted and **→** completed; in
Uncompleted, **→** brings it back to Tasks; in Completed, **×** brings it
back. Each move is one persisted change of the placement's TaskExecution --
the lifecycle actions `complete` (allowed from `scheduled`: done without
timing, so no duration metrics), `skip` and `reopen` -- in one transaction,
with the execution version on screen as the precondition (a change made on
another device is reported and reloaded, never overwritten). There is no
second status store; see docs/execution-rescheduling.md section 2.

**Make Schedule again keeps every status.** The column is read from the
placement's execution by placement id -- never by task name or position -- and
generation never writes executions: placements whose attempts were answered
(completed, skipped) or started are kept in place with their ids by every
generation, and an incremental Make Schedule keeps every placement that still
fits. Only newly placed tasks appear, in Tasks. Duplicate names and the
occurrences of a recurring task each have their own placement and status. A
removed task leaves the board; its execution stays as history.

Viewing the board writes nothing; the first move creates the placement's
execution (never a second one).

The Productivity page adds "Schedule follow-through" (the schedule cohort of
docs/analytics.md: due completion with numerator and denominator, the date
basis and reporting timezone, reschedules, workload, signals and
underestimation) and "History" (last 7/30/90 days, filtered by status and
category; each entry shows the original plan, the move lineage, the outcome,
actual times and sessions -- also for work whose task or placement is no
longer on the schedule). Its Data section says where history is stored and
what deleting it does in the active mode (this device only; this device and
the account's synchronized copy; or the server database in direct mode).

## Limitations

- On a daylight-saving change day, positions on the strip are elapsed minutes
  from local midnight, while the times are wall-clock times.
- Reasons for unplaced tasks are not stored. After a restart only their count
  is known.
