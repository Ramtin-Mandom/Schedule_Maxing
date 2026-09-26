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

- The app opens on **today's real date in the planning timezone**
  (`SCHEDULE_MAXING_TIMEZONE`, default UTC), not the computer's local date.
- Saved data is loaded at once.
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
- The part of the day outside the effective **scheduling window** is shaded.
  **Free time** inside the window is drawn with a dashed outline. Free time is
  calculated for display only and is never saved as a task.
- Items that overlap (possible only for out-of-date work) get their own row.
- **Keyboard:** Tab into the strip. Left/Right/Home/End select an item. Enter
  (or a double-click) edits it, Delete removes it, and the Menu key or
  Shift+F10 opens its actions.
- The selected item's details are written out below the strip, with h:mm
  AM/PM times, duration, kind and category. With nothing selected, the line
  lists the window and the free time.
- The task list (**Tasks on this date**) offers the same actions.

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

## Limitations

- On a daylight-saving change day, positions on the strip are elapsed minutes
  from local midnight, while the times are wall-clock times.
- Reasons for unplaced tasks are not stored. After a restart only their count
  is known.
