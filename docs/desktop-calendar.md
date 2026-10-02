# Week and Month (Milestone 4, Prompt 5)

Week and Month are real calendar views in the planning timezone (the
computer's own zone unless `SCHEDULE_MAXING_TIMEZONE` is set). Above the
calendar, the selected day's **Day Window** can be changed for that date
([desktop-day.md](desktop-day.md#the-day-window)). The task form has no date
field: a new task gets the selected day. You don't make schedules here: you
pick a day and **Open Day**, and the Day page makes its schedule
([desktop-day.md](desktop-day.md)).

| Layer | Module |
| --- | --- |
| Calendar arithmetic (Tk-free) | `app/ui/calendar_model.py` (`Period`, `month_grid`, `week_of`, `move_months`) |
| Presenter (Tk-free) | `app/ui/calendar_controller.py` (`CalendarController`, `CalendarSnapshot`, `CalendarDay`) |
| Drawing | `app/ui/calendar_view.py` (`CalendarView`) |
| Page | `app/ui/calendar_page.py` (`CalendarPage`, one class for both) |

## The calendar

- **Weeks start on Monday**, like the allocator's `week_dates` and the
  earlier pages.
- **Week** opens on the current week, and **Month** on the current month,
  of *today in the planning timezone*. Each page remembers its selected day
  while you move between pages.
- **Months are real months**: 28 days, 29 in a leap February, 30 or 31. They
  are never "30 days from a start date".
  - The grid is whole Monday-first weeks (4 to 6 rows), so every date sits
    under its own weekday.
  - Days of the neighbouring months are drawn on another background and
    labelled with their month ("Aug 31", "Oct 1").
  - They are never part of the month's reset or task list.
- **Moving by month** keeps the day of the month, clamped to the target
  month (Jan 31 → Feb 28, or 29 in a leap year). It rolls over into the next
  or previous year.
- **Month** shows its name and year prominently. A **Month** choice lists
  the current year's twelve months.
- **Go to date** jumps to any date's week or month. Previous, Today and Next
  are always there.

## What a day shows

- **Fixed blocks and saved placements** at their actual times, in time
  order, in their category's colors. On Week they sit on a time axis; on
  Month each cell lists them ("9:00a Lecture").
- **Tasks that are not scheduled**, in the order they were entered,
  labelled "not scheduled". They are never given a time. On Week they are
  listed in a band above the time axis, which stays in view while the axis
  scrolls.
- A task planned for the day but scheduled on another date says where.
- **Crowded days** show "+N more". The selected day's full, ordered details
  (exact h:mm AM/PM times) are listed below the calendar.
- The date's freshness ("Current" or "Out of date") comes from the saved
  provenance, as on Day.
- **Past days** are drawn on a quieter background with muted text. They
  stay readable and fully visible; nothing is hidden or deleted.
- Today is marked, and the selected day has an accent outline.

## Selecting and opening a day

- **Selecting:** a click selects a day. So do the keyboard keys on the
  focused calendar: Left/Right move one day, Up/Down one week on Month (they
  scroll on Week), and Home/End jump to the first or last day. Selecting a
  day outside the shown period moves to its week or month.
- **Opening:** the **Open Day** button (or Enter) opens the selected day on
  the Day page. A double-click is only a shortcut. **Open date in Day
  Schedule** in the task list does the same for a row.
- **Coming back:** the Day page then shows **‹ Back to Week/Month (…)**.
  Back returns to this page with its week or month and its selected day.
- **Adding tasks:** the task form below the calendar starts on the selected
  date. Selecting another day moves the form's date unless you are editing.

## Selected day, bulk actions and historical colours

The right side of Week and Month is the **Selected day** panel
(`app/ui/selected_day_panel.py`, model `app/ui/day_outcomes.py`); it replaces
the former "Tasks this week/month" list. For the selected date it lists the
**scheduled** tasks (live placements) with their outcome -- Completed,
Uncompleted, or Tasks (pending) -- and the day's counts, planned hours and
points. Editing single tasks happens on the Day page (Open Day).

- **All Tasks Complete** marks every scheduled task of the date completed;
  **No Tasks Complete** marks them all uncompleted. One transaction
  (`ExecutionController.set_outcomes`); the same TaskExecution state as the
  Day page's board, so Day, Week and Month always agree. Tasks the scheduler
  did not place are never touched; a cancelled attempt is left as it is.
- **Past dates are tinted** by the one shared classification
  (`app/productivity/day_summary.classify_day`, colours
  `theme.DAY_STATUS_FILLS`). Only in-period dates before today; today, future
  dates and a month grid's neighbouring days keep their look. Precedence:
  no scheduled tasks (neutral dark tint) → uncompleted ≥ 80% (dark red) →
  completed ≥ 80% (dark green) → uncompleted ≥ 60% (light red) → completed
  ≥ 60% (light green) → pending ≥ 50% (light white) → otherwise yellow.
  Percentages are of the date's scheduled tasks, compared exactly.
- The colour is never the only cue: each coloured day says its class in
  words, the legend under the calendar lists every colour's meaning, and the
  panel names it.
- Reads are batched: the whole week or month grid costs one planning range
  load and one execution query (never one read per date).

## Reset Week / Month

- Resets only the period's own dates: the seven days of the week, or the 1st
  to the last day of the month.
- It uses the same previewed, token-confirmed `reset_range` as Reset Day.
  Before anything is deleted it lists the tasks, fixed blocks, scheduled
  entries, schedule records and date preferences that go. It also lists
  scheduled entries of those tasks outside the period that go with them, and
  repeating tasks that are kept.
- Cancelling deletes nothing.
- Confirming applies exactly the preview, atomically. It is refused, with
  nothing deleted, if anything changed after the preview.
- The reset is refused when a task outside the period depends on a task
  inside it; the message names them.
- Undated tasks, other dates, projects, your defaults and all execution
  history are kept.

## Loading and staying current

- One load reads one bounded range: a week, or at most 42 days of month
  grid. It uses one `load_range` and the persisted freshness of those dates.
  Nothing is read per pixel or per cell.
- **Moving** to another week or month loads in a background worker. Each
  load carries its period; a result for a period the page no longer shows
  is dropped. A result from before an account switch is dropped too
  (`AppServices.workspace_guard`).
- **Refreshing:** edits, imports, resets and returning to the page re-read
  it (`on_show`). So does a sync that pulled changes (the app reloads the
  visible page).
- **Resizing:** painting is coalesced, and a width change repaints once
  after the resize burst.
- **Narrow windows:** the header wraps and the task form, actions and task
  list stack. The calendar keeps a minimum column width and scrolls sideways
  instead of clipping.

## Limitations

- The earlier Week/Month **Make Schedule** (generate a whole range at once)
  is no longer on these pages. The same operation stays available to
  headless callers (`PlanningController.schedule_range`,
  `SchedulePageController.make_schedule`) and the CLI.
- CSV import/export is on the Day page (canonical v2).
- Recurring series are listed as "repeats" rows (or "needs setup"); their
  occurrences appear as "repeat" rows once a range is scheduled -- Make
  Schedule materializes them first ([recurrence.md](recurrence.md)).
- On a daylight-saving change day, positions on the Week time axis are
  elapsed minutes from local midnight.
