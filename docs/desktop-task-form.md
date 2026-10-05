# The task form (Milestone 4, Prompt 3)

Day, Week and Month use one reusable form (`app/ui/task_editor.py`). Its
rules live in `app/ui/task_form_model.py` and `app/ui/time_fields.py`, and
saving goes through the page presenter (`SchedulePageController.save_draft`)
and the planning services. Projects will use the same form.

You never type ids, owners, timestamps or versions. Dependencies and projects
are chosen by name but kept by id, so duplicate task names work: the choices
show the date, and a short id suffix where names and dates still collide.

## Flexible task

| Field | Meaning |
| --- | --- |
| Name, Category | Name is required. Category starts at "(none)", which saves as `other`. The list is the built-in categories plus those added in Settings (`app/ui/task_defaults.py`). A category the list lacks, for example an imported one, is kept and offered for that record. It is never renamed |
| Project | Next to Category. Starts at "None" and lists the workspace's live projects; chosen by name, kept by id |
| Date | Not typed. A new task gets the date the page has selected -- Day: the date shown (today unless opened for another date); Week/Month: the selected day; the form does not show it. That real date becomes the first preferred date. Editing keeps the record's own date (and its other preferred dates); an undated imported task stays undated |
| Pinned date | No longer a form control. A new task is not pinned; editing a task that is pinned (`required_date`, a hard constraint) keeps it pinned |
| Required | The task must be scheduled (`required`) |
| Duration | Whole minutes, from 1 minute to 24 h: `13`, `13 min`, `1 h 13 min`, `1h13m`, `1:13` |
| Priority | 1 (low) to 10 (high) |
| Points | 0 to 1000, default 1, typed or changed by 10 with − / +: what finishing the task is worth to the user, for productivity analytics. Not a scheduling input (the inputs fingerprint excludes it) and never the optimizer's placement `score`. Each execution snapshots the task's points when it is created |
| Tags | An ordered list. **Enter** adds the typed tag as a chip and never submits the task. A chip's ✕ removes it, and **Backspace** in the empty tag field removes the last one. The chips stay on one row and scroll sideways when they are wider than the form. Blank and repeated tags are ignored. There is no mandatory tag. The scoring adapter still reads only the first tag, and the engine is unchanged |
| More options → Preferred from / until | An optional preferred window. Leave both empty for "any time" |
| More options → Deadline | Date and time. Both are needed if either is given |
| More options → Depends on | Chosen by name, kept by id |
| Use default values | Fills the name (if empty), duration, priority and points from the chosen category's defaults, or the general ones when no category is chosen. Out of the box: priority 5, 60 minutes, 20 points, named after the category. Changed in Settings, stored in `task_defaults.json` beside the database (this device only); "Reset All Task Data" removes added categories and changed values |
| More options → Task type | The reusable type the task counts under on the Productivity page. "(its own type)" keeps the stored type (a new task gets one of its own); pick an existing type to share it, or "New type..." and a name to create one. Independent of category and tags; an occurrence always has its series' type |

## Fixed block

Label, category, start and end, on the page's selected date (as for a task, the date is not typed).

- A fixed block is not a disguised flexible task, and editing never turns
  one kind into the other. To change the kind, remove the record and add the
  other kind.
- Before anything is written, the services check that the block ends after it
  starts, lies inside the date's effective day window, and does not overlap
  another block. When editing, the block is not compared with itself.
- A refusal names the other block in local time, for example: "overlaps the
  fixed block “Lecture” (9:00 AM – 10:30 AM on Wed Jun 5)". The form keeps
  everything you typed, and nothing is written.

## Times

- Every clock time (preferred window, deadline time, fixed-block start/end,
  the Day Window, the Settings default day window, category preferred times,
  Reschedule) is entered in one shared input (`app/ui/clock_input.py`):
  **[ Hour ] : [ Minute ] [ AM/PM ]**. The hour is 1-12, the minute 00-59
  (shown with two digits once you leave the input), and the AM/PM button
  starts at AM and toggles on each click (typing `a` or `p` in either field
  sets it too). Typing a colon, two hour digits, or an hour digit 2-9 moves to
  the minutes. There are no spinners and you never type minutes from midnight.
- The conversions are `app/ui/time_fields.py`'s `clock_to_minutes`,
  `minutes_to_clock` and `parse_clock_parts`: 12:00 AM = 0, 12:30 AM = 30,
  1:00 AM = 60, 12:00 PM = 720, 1:15 PM = 795, 11:59 PM = 1439. Times are
  exact to the minute; nothing is rounded to a grid.
- Noon is `12:00 PM`, and the start of the day is `12:00 AM`. The optional
  deadline date is still typed as YYYY-MM-DD.
- An **end** time of `12:00 AM` means the following midnight; the input
  says "next day" beside it. An interval can never end at the start of its
  own day.
- Times are in the page's planning timezone, which the form shows. An
  existing block in another zone is edited in its own zone.

## Limitations

These inputs are refused with a message and are never rounded or silently
adjusted:

- **Overnight intervals.** An end before the start, or an end after the next
  midnight, is not supported. Split the interval at midnight.
- **Daylight-saving gaps and folds.** A time the clocks skip (for example
  2:30 AM on a spring-forward date) or repeat (1:30 AM on a fall-back date)
  is refused for a deadline or block, with a request to choose another time.
- **Blocks across a daylight-saving change.** A block that spans the change
  is refused, with a request to split it at the change.
- **Recurrence.** "Repeats" (in More options) makes the task a recurring
  series starting on the page's date in the page's time zone: daily, weekly
  (weekdays), monthly (day of month; months without it are skipped), every N,
  ending never, on a date or after N times. A template saved before series
  repeated shows "Needs setup" and is configured only when Repeats is chosen.
  Saving an edited occurrence asks whether the change applies to only it, to
  it and every later occurrence, or to the entire series; removing one offers
  skip, delete it, delete it and later ones, or the entire series. See
  [recurrence.md](recurrence.md).
- **Task list times.** Since the Day rebuild (Prompt 4) the task lists, the
  Execute choices and the schedule strips show h:mm AM/PM times.

## Actions on tasks

Actions are on the Day page: the timeline (fixed blocks and scheduled tasks)
and the available-task buttons (unscheduled tasks). They are shared by every
page (`app/ui/task_actions.py`); Week and Month open a date there with Open Day.

- **Enter** (or a double-click) on a timeline item, or an available-task
  button, edits it.
- **Delete** on a timeline item removes it. The Menu key or **Shift+F10**
  opens the same actions.

Removal first asks for confirmation and says what goes with the task: its
saved schedule entries do, while execution history is kept. A task that
others depend on is not removed; the refusal names those tasks. An edit made
from an outdated row, one that changed elsewhere since the page was drawn, is
refused rather than overwriting the newer version. The old half-hour legacy
form API (`SchedulePageController.submit_task_form`) remains only for
existing headless callers.
