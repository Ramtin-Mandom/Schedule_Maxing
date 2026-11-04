# The task form (Milestone 4, Prompt 3)

Day, Week and Month use one reusable form (`app/ui/task_editor.py`). Its
rules live in `app/ui/task_form_model.py` and `app/ui/time_fields.py`, and
saving goes through the page presenter (`SchedulePageController.save_draft`)
and the planning services. The Projects page uses the same form, with a Date
field where the others have the project choice
([desktop-projects-allocation.md](desktop-projects-allocation.md)).

You never type ids, owners, timestamps or versions. Projects are chosen by
name but kept by id, so duplicate names work.

The form's **Task type** switch chooses **Flexible**, **Fixed** or **To Do**
and shows only that type's fields; a saved record is built from the shown
type's fields alone, so a value typed for another type is never saved. A
record keeps its type: while one is edited the other two choices are
disabled. To change the type, remove the record and add the other one.

| Type | Stored as | Fields |
| --- | --- | --- |
| Flexible | `Task`, `kind = flexible` | Name, category, project, points, duration, preferred time, required, tags |
| Fixed | `FixedBlock` | Label, category, points, start, end |
| To Do | `Task`, `kind = todo` | Name, category, points, tags |

There are **no advanced settings**: the form has no preferred window,
deadline, task-type or repeat controls. A record that already has one of
those (saved by an earlier version, imported from CSV, or synchronized from
another client) keeps it unchanged when it is edited here, and the scheduler
still honours deadlines and recurring series. Tasks have no priority or
dependencies at all (removed everywhere, ADR-048).

## Flexible task

| Field | Meaning |
| --- | --- |
| Name, Category | Name is required. Category starts at "(none)", which saves as `other`. The list is the built-in categories plus those added in Settings (`app/ui/task_defaults.py`). A category the list lacks, for example an imported one, is kept and offered for that record. It is never renamed |
| Project | Next to Category. Starts at "None" and lists the workspace's live projects; chosen by name, kept by id |
| Date | Not typed. A new task gets the date the page has selected -- Day: the date shown (today unless opened for another date); Week/Month: the selected day; the form does not show it. That real date becomes the first preferred date. Editing keeps the record's own date (and its other preferred dates); an undated imported task stays undated |
| Pinned date | No longer a form control. A new task is not pinned; editing a task that is pinned (`required_date`, a hard constraint) keeps it pinned |
| Required | The task must be scheduled (`required`) |
| Duration | Whole minutes, from 1 minute to 24 h: `13`, `13 min`, `1 h 13 min`, `1h13m`, `1:13` |
| Preferred time | Early, Mid or Late: a third of the date's day window (not midnight to midnight). The scheduler places the task there whenever it fits, and looks elsewhere only when that third has no valid placement |
| Points | 0 to 1000, default 1, typed or changed by 10 with − / +: what finishing the task is worth to the user, for productivity analytics. Never the optimizer's placement `score`, but the first key of the week/month allocation order (higher points get a date first, ADR-049), so the inputs fingerprint includes it. Each execution snapshots the task's points when it is created |
| Tags | Flexible tasks and To Dos. An ordered list. **Enter** adds the typed tag as a chip and never submits the task. A chip's ✕ removes it, and **Backspace** in the empty tag field removes the last one. The chips stay on one row and scroll sideways when they are wider than the form. Blank and repeated tags are ignored. There is no mandatory tag. The scoring adapter still reads only the first tag, and the engine is unchanged |
| Use default values | Fills the name (if empty), duration and points from the chosen category's defaults, or the general ones when no category is chosen. Out of the box: 60 minutes, 20 points, named after the category. Changed in Settings, stored in `task_defaults.json` beside the database (this device only); "Reset All Task Data" removes added categories and changed values |

**Priority is gone.** Tasks have no priority anywhere (local schema v19,
server revision 0021); the sync wire still carries a constant `priority` of 5
for 1.3.0 clients. What a task is worth to you is its **points**.

**Preferred time replaces the preferred window.** `Task.preferred_time` is
`early`, `mid` or `late`; `preferred_third_bounds` divides the date's day
window into three consecutive parts (an 8:00 AM - 11:00 PM day: 8:00 AM -
1:00 PM, 1:00 PM - 6:00 PM, 6:00 PM - 11:00 PM). A new task starts at Mid. A
custom `preferred_time_window` from an older record or an import is legacy
data, read only for a task that has no Early / Mid / Late choice; a
category's preferred time (Settings, Day Preferences) applies to those tasks
only.

## Fixed block

Label, category, points, start and end, on the page's selected date (as for a
task, the date is not typed). It has no project and no tags.

- A fixed block is completed on the Day page's board like scheduled work, for
  its points; they never affect the schedule. A block saved before fixed
  blocks had points is worth 0 points and counts as completed.
- A fixed block is not a disguised flexible task, and editing never turns
  one kind into the other.
- Before anything is written, the services check that the block ends after it
  starts, lies inside the date's effective day window, and does not overlap
  another block. When editing, the block is not compared with itself.
- A refusal names the other block in local time, for example: "overlaps the
  fixed block “Lecture” (9:00 AM – 10:30 AM on Wed Jun 5)". The form keeps
  everything you typed, and nothing is written.

## To Do

Name, category, points and tags. A checklist item: no duration and no time,
and it belongs to no project. A new To Do belongs to the page's selected date
(an edited one keeps its own); it is never scheduled and never drawn on the
timeline.

- It is a sticky note under the Day timeline
  ([desktop-day.md](desktop-day.md#to-do)): **Done** completes it for its
  points, unticking takes the completion back, the small ✕ removes it, and
  selecting its name opens it in the form.
- Its completion and points count on the day the To Do belongs to, whenever
  it was actually ticked.
- A To Do never repeats.

## Times

- Every clock time (fixed-block start/end, the Day Window, the Settings
  default day window, category preferred times, Reschedule) is entered in one shared input (`app/ui/clock_input.py`):
  **[ Hour ] : [ Minute ] [ AM/PM ]**. The hour is 1-12, the minute 00-59
  (shown with two digits once you leave the input), and the AM/PM button
  starts at AM and toggles on each click (typing `a` or `p` in either field
  sets it too). Typing a colon, two hour digits, or an hour digit 2-9 moves to
  the minutes. There are no spinners and you never type minutes from midnight.
- The conversions are `app/ui/time_fields.py`'s `clock_to_minutes`,
  `minutes_to_clock` and `parse_clock_parts`: 12:00 AM = 0, 12:30 AM = 30,
  1:00 AM = 60, 12:00 PM = 720, 1:15 PM = 795, 11:59 PM = 1439. Times are
  exact to the minute; nothing is rounded to a grid.
- Noon is `12:00 PM`, and the start of the day is `12:00 AM`.
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
  is refused for a block, with a request to choose another time.
- **Blocks across a daylight-saving change.** A block that spans the change
  is refused, with a request to split it at the change.
- **Recurrence.** The form no longer creates a repeating task. An existing
  series or occurrence (older data, an import, another client) still works:
  the form says what it is, saving an edited occurrence asks whether the
  change applies to only it, to it and every later occurrence, or to the
  entire series, and removing one offers skip, delete it, delete it and later
  ones, or the entire series. See [recurrence.md](recurrence.md).
- **Task list times.** Since the Day rebuild (Prompt 4) the task lists, the
  Execute choices and the schedule strips show h:mm AM/PM times.

## Actions on tasks

Actions are on the Day page: the timeline (fixed blocks and scheduled tasks),
the available-task buttons (unscheduled tasks) and the To Do notes. They are shared by every
page (`app/ui/task_actions.py`); Week and Month open a date there with Open Day.

- **Enter** (or a double-click) on a timeline item, or an available-task
  button, edits it.
- **Delete** on a timeline item removes it. The Menu key or **Shift+F10**
  opens the same actions.
- The small **✕** on an available task or a To Do removes it, and while a
  record is open in the form, **Remove** does.

Removal first asks for confirmation and says what goes with the record: a
task's saved schedule entries, completions and points; a fixed block's or a
To Do's completion and points. A task that
others depend on is not removed; the refusal names those tasks. An edit made
from an outdated row, one that changed elsewhere since the page was drawn, is
refused rather than overwriting the newer version. The old half-hour legacy
form API (`SchedulePageController.submit_task_form`) remains only for
existing headless callers.
