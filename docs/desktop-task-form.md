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
| Name, Category | Required. A category the list lacks, for example an imported one, is kept and offered for that record. It is never renamed |
| Date | Becomes the first preferred date. Editing replaces the previously displayed date and preserves additional preferred dates; the calendar uses the earliest preferred date. Clearing this field removes all preferred dates, making the task undated |
| Only on this date | Pins the task to the date (`required_date`, a hard constraint). Other preferred dates it already had are kept |
| Required | The task must be scheduled (`required`) |
| Duration | Whole minutes, from 1 minute to 24 h: `13`, `13 min`, `1 h 13 min`, `1h13m`, `1:13` |
| Priority | 1 (low) to 10 (high) |
| Tags | An ordered list. **Enter** adds the typed tag as a chip and never submits the task. A chip's ✕ removes it, and **Backspace** in the empty tag field removes the last one. Blank and repeated tags are ignored. There is no mandatory tag. The scoring adapter still reads only the first tag, and the engine is unchanged |
| More options → Preferred from / until | An optional preferred window. Leave both empty for "any time" |
| More options → Deadline | Date and time. Both are needed if either is given |
| More options → Project, Depends on | Chosen by name, kept by id |

## Fixed block

Label, category, date, start and end.

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

- Times display as **h:mm AM/PM** and are exact to the minute. You never type
  minutes from midnight.
- Accepted input: `10:13`, `10:13 AM`, `10:13pm`, `10 am`, `22:13`, `noon`,
  `midnight`.
- **Up/Down**, the mouse wheel, or the ▲▼ buttons step a time by 1 minute, or
  15 with **Shift**. Typing and stepping give the same values. Dates step by
  a day, or a week with Shift.
- Noon is `12:00 PM`, and the start of the day is `12:00 AM`.
- An **end** time of `12:00 AM` (or `midnight` or `24:00`) means the
  following midnight and is shown as `12:00 AM (next day)`. An interval can
  never end at the start of its own day.
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
- **Recurrence.** Stored recurrence is preserved through edits, but there is
  no Repeat control, because occurrences are not expanded yet.
- **Task list times.** Since the Day rebuild (Prompt 4) the task lists, the
  Execute choices and the schedule strips show h:mm AM/PM times.

## Actions on tasks

Actions are in the task list (**Tasks this week/month** or **Tasks on this
date**; on the Day page also on the timeline and the available-task buttons). They are shared by
every page (`app/ui/task_actions.py`):

- **Enter** or **Edit Selected** edits the selected task.
- **Delete** or **Remove Selected** removes it. The Menu key or
  **Shift+F10** opens the same actions.

Removal first asks for confirmation and says what goes with the task: its
saved schedule entries do, while execution history is kept. A task that
others depend on is not removed; the refusal names those tasks. An edit made
from an outdated row, one that changed elsewhere since the page was drawn, is
refused rather than overwriting the newer version. The old half-hour legacy
form API (`SchedulePageController.submit_task_form`) remains only for
existing headless callers.
