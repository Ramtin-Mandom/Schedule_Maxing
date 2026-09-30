"""
app/ui/guide_content.py

The text of the in-app "How to Use" page (app/ui/guide_page.py), Tk-free so
it is tested headlessly (tests/ui/test_guide.py). Each GuideSection is a
heading and blocks: a paragraph (str) or a bullet list (tuple of str).

Keep it true to the app: describe only what exists. The "day_status_colours"
section is where the day-status colours are explained; extend it when those
colours change.
"""

from __future__ import annotations

from dataclasses import dataclass

Block = str | tuple[str, ...]


@dataclass(frozen=True)
class GuideSection:
    key: str
    title: str
    blocks: tuple[Block, ...]


GUIDE_TITLE = "How to Use"
GUIDE_SUBTITLE = ("Everything Schedule Maxing does, and how to use it: from adding your first task to making, "
                  "following and syncing a schedule.")

GUIDE_SECTIONS: tuple[GuideSection, ...] = (
    GuideSection("overview", "What Schedule Maxing does", (
        "Schedule Maxing turns a list of things to do into a realistic plan for each day. You tell it what is "
        "fixed (classes, work, meals, sleep) and what is flexible (study, errands, exercise). It keeps the fixed "
        "blocks exactly where they are and places the flexible tasks into the free time around them, choosing "
        "the times that score best for your priorities and preferences.",
        "Rules are never bent to get a better score: tasks never overlap fixed blocks or each other, stay inside "
        "the day window, and come after the tasks they depend on. When something does not fit, it is left "
        "unscheduled and you are told why.",
        "The app knows today's date from your computer's clock and time zone. You never type today's date.",
    )),
    GuideSection("day_view", "Day view", (
        "Day Schedule is today's page. Opening it from the navigation always shows today; the date controls "
        "(Previous day, Today, Next day) and Open Day on the Week and Month pages show other dates, with a Back "
        "button to where you came from.",
        (
            "Day Window: the start and end of the usable day, above the schedule.",
            "Timeline: fixed blocks in their category colour, scheduled tasks at their exact minutes, and the "
            "free gaps between them.",
            "Available tasks: tasks for this date (and tasks with no date) that are not on the schedule yet, "
            "with the reason when it is known. Select one to edit it.",
            "Add a task, Make Schedule, the engine choice, Day Preferences, CSV import/export and Reset Day.",
            "Uncompleted | Tasks | Completed: the scheduled tasks of the date, and what happened to each.",
        ),
    )),
    GuideSection("week_view", "Week view", (
        "Week Schedule shows Monday to Sunday of a real calendar week. Click a day (or move with the arrow keys) "
        "to select it: the Day Window, the day's details and the task form all follow the selected day.",
        (
            "Previous week, Today and Next week move between weeks; Go to date jumps to any date.",
            "Open Day (or a double-click) opens the selected date on the Day page, where schedules are made.",
            "The Selected day panel lists the day's scheduled tasks and what happened to each (Completed, "
            "Uncompleted or still in Tasks), with its counts, planned hours and points.",
            "All Tasks Complete marks every scheduled task of the selected day completed; No Tasks Complete marks "
            "them all uncompleted. It is the same status the Day page shows, and tasks the scheduler could not "
            "place are never affected.",
            "Past days are coloured by how they went (see Day status colours).",
            "Show project filters what is displayed; fixed blocks always stay visible.",
            "Reset Week clears this week's own dates after showing exactly what would go.",
        ),
    )),
    GuideSection("month_view", "Month view", (
        "Month Schedule shows a real calendar month (28 to 31 days, never a fixed 30). Pick a month from the "
        "Month list or step with Previous/Next month. Selecting a day works exactly as on the Week page: the "
        "Day Window, details, task form and Selected day panel (with All Tasks Complete and No Tasks Complete) "
        "follow it, and Open Day schedules it. Past days are coloured the same way as on the Week page.",
    )),
    GuideSection("adding_tasks", "Adding tasks", (
        "The Add a task form is the same on Day, Week and Month. It has no date field: a new task always belongs "
        "to the date the page has selected. On the Day page that is the date shown (today unless you opened "
        "another); on Week and Month it is the day you selected. The form shows that date above the fields.",
        (
            "Name, category, duration (like 45 min or 1 h 15 min) and priority are all you need.",
            "Points (0 to 1000, default 1) is what finishing the task is worth to you. It is saved for your "
            "productivity statistics and never changes where the scheduler puts the task.",
            "Times are entered as [hour] : [minute] [AM/PM]. Type the hour (1-12) and minute (00-59); click the "
            "AM/PM button, or type a or p, to switch. An end time of 12:00 AM means midnight at the end of the day.",
            "Required: the task must be scheduled; if it cannot be, the schedule is not saved and you are told why.",
            "Only on this date: the task may be placed on its date only (otherwise it can move when planning a "
            "week or month).",
            "More options: preferred time, deadline, project and dependencies.",
            "Editing a task keeps its own date; the form says so while you edit.",
        ),
    )),
    GuideSection("fixed_blocks", "Fixed tasks (blocks)", (
        "A fixed block is something that happens at a set time: a class, a shift, a meal, sleep. Choose Fixed "
        "block in the form, then give it a label, a category and its start and end. The scheduler never moves "
        "a fixed block and never places anything on top of it.",
        (
            "Two fixed blocks cannot overlap; touching end-to-start is fine.",
            "A fixed block must lie inside the date's day window.",
            "A block cannot run past midnight; split it into two blocks at midnight.",
        ),
    )),
    GuideSection("flexible_tasks", "Flexible tasks", (
        "A flexible task has a duration but no set time. The scheduler finds the best free time for it on its "
        "date, around the fixed blocks and inside the day window. Times are exact to the minute.",
    )),
    GuideSection("preferred_times", "Preferred times", (
        "Under More options, Preferred from / Preferred until tell the scheduler when you would like a task to "
        "happen, for example a workout in the morning. It is a preference, not a rule: the task still goes "
        "elsewhere when that time is taken, just with a lower score.",
        "A category can have a preferred time too (Settings or Day Preferences); a task's own preferred time "
        "wins over its category's.",
    )),
    GuideSection("categories", "Categories", (
        "Every task and fixed block has a category: study, work, class, exercise, sleep, food, event, "
        "entertainment, errand or other. Categories give items their colour on the schedule and can have their "
        "own importance (a multiplier on priority; 1 is neutral) and preferred time in Settings.",
    )),
    GuideSection("priority_points", "Priority, the scheduler's score, and points", (
        "Priority goes from 1 (low) to 10 (high). When the scheduler compares possible times for a task, it "
        "gives each one a score:",
        (
            "priority, multiplied by the priority weight and the category's importance;",
            "a bonus for being close to the preferred time;",
            "a bonus for being near tasks with the same or related tags;",
            "a penalty for leaving awkward small gaps (the fragmentation penalty).",
        ),
        "The highest-scoring valid time wins, so high-priority tasks get the best times first. The weights are "
        "in Settings under Default scheduling preferences.",
        "Points are different: they are your own value for a task (set in the form), counted when you complete "
        "it. The scheduler never reads them, and its score is never your points.",
    )),
    GuideSection("dependencies", "Dependencies", (
        "Under More options, Depends on lists your other tasks. A task that depends on another is only "
        "scheduled after that task has finished. Dependencies are kept by task, not by name, so tasks with the "
        "same name stay distinct. Circular dependencies (A needs B, B needs A) cannot be scheduled; Make "
        "Schedule and planning report them so you can remove one.",
    )),
    GuideSection("make_schedule", "Make Schedule and the scheduling engine", (
        "On the Day page, Make Schedule places the date's flexible tasks. The Engine choice beside it decides "
        "how:",
        (
            "Normal: tasks may start at any minute, at the best-scoring time.",
            "ADHD friendly: tasks longer than 30 minutes start on the quarter hour, and filling short gaps with "
            "short tasks is rewarded. Durations never change.",
        ),
        "The first run places everything that fits. If nothing changed since, Make Schedule says the schedule is "
        "already current. After a change, it keeps the work that still fits and adds the rest. Regenerate "
        "replaces all work that has not started yet; started or finished work always stays.",
    )),
    GuideSection("scheduled_unscheduled", "Scheduled and unscheduled tasks", (
        "Scheduled tasks appear on the timeline at their exact times. A task the scheduler could not place "
        "(no free time long enough, outside its date, waiting on a dependency) stays in Available tasks with "
        "the reason from the last run. It is not lost: free up time, shorten it or widen the day window, then "
        "Make Schedule again. A required task that cannot be placed stops the run, and nothing is saved.",
        "An unscheduled task is never counted as Uncompleted: it was never on the schedule.",
    )),
    GuideSection("task_workflow", "Uncompleted, Tasks and Completed", (
        "On the Day page, every task the schedule placed on the date starts in the middle column, Tasks. Say what "
        "happened with the buttons on each card:",
        (
            "In Tasks: × moves the task to Uncompleted (it did not happen), → moves it to Completed.",
            "In Uncompleted: → brings the task back to Tasks.",
            "In Completed: × brings the task back to Tasks.",
            "Every change is saved at once and synchronized: it is still there after a restart, after signing in "
            "again and on your other devices.",
            "Make Schedule again never moves a task between these columns: completed and uncompleted tasks keep "
            "their place and status, and only newly scheduled tasks appear in Tasks.",
            "Two tasks with the same name, or the days of a repeating task, each keep their own status.",
            "Completed and uncompleted work, with planned versus actual times, appears on the Productivity page "
            "(Schedule follow-through and History).",
        ),
    )),
    GuideSection("day_window", "Changing the day start and end", (
        "The Day Window above the Day, Week and Month schedules sets when the usable day starts and ends for "
        "the selected date. Enter a new Start and End and choose Apply to this date: only that date changes, "
        "and Make Schedule then places flexible tasks only inside it.",
        (
            "Every date starts with the default day window from Settings.",
            "Changing the default in Settings changes every date without its own window; dates you customised "
            "keep theirs.",
            "Use default removes a date's own window so it follows Settings again.",
            "A window must end after it starts and must contain the date's fixed blocks; otherwise nothing is "
            "saved and you are told what to change.",
        ),
    )),
    GuideSection("settings", "Settings", (
        (
            "Appearance: light or dark theme and interface size, saved on this computer.",
            "Default scheduling preferences: the default engine, the default day window, scoring weights, and "
            "each category's importance and preferred time. Each value shows where it comes from; Use inherited "
            "goes back to the app default.",
            "Day Preferences (on the Day page) changes the same values for one date only.",
        ),
    )),
    GuideSection("synchronization", "Synchronization", (
        "Your data is saved on this computer first, so the app works offline. Sign in on the Account page to "
        "sync with the Schedule Maxing server: changes you make wait safely until they are sent, the status "
        "bar at the top shows what is waiting, and Sync now sends them at once. Changes from your other devices "
        "come back the same way, including each date's day window. If the same record changed in two places, "
        "the Account page shows the conflict and lets you choose.",
        "With direct PostgreSQL storage everything is saved straight to the database, so there is nothing to "
        "sync.",
    )),
    GuideSection("reset", "Reset", (
        "Reset Day, Reset Week and Reset Month clear the tasks planned for those dates, their fixed blocks, "
        "their scheduled entries and their own preferences (including a custom day window). You always see "
        "exactly what would be deleted and confirm first.",
        (
            "Kept: tasks with no date, tasks planned on other dates, projects, your default preferences and all "
            "execution history.",
            "Repeating tasks are kept; only their entries on the reset dates go.",
            "A reset is refused, with nothing deleted, while tasks outside the dates depend on tasks inside them.",
        ),
        "Settings > Reset All Task Data removes everything at once: all tasks, fixed blocks, projects, schedules "
        "and completion history -- from your account on the server and every device when you are signed in, "
        "otherwise from this computer. It asks first and cannot be undone. Your account, sign-in and settings "
        "(including default and per-day day windows) are kept. If the server cannot be reached, nothing is "
        "deleted anywhere.",
    )),
    GuideSection("day_status_colours", "Day status colours", (
        "On the Week and Month pages, every PAST date is coloured by how its day went. Only tasks that were "
        "actually scheduled on that date count -- a task the scheduler could not place does not change the "
        "colour. Today and future dates keep their normal look. Each coloured day also says its meaning in "
        "words (in the day, in the legend under the calendar and in the Selected day panel).",
        (
            "Neutral, dark tint: no scheduled tasks on that day.",
            "Light white: 50% or more of the scheduled tasks are still pending (in Tasks).",
            "Dark red: 80% or more uncompleted.",
            "Light red: 60% or more uncompleted.",
            "Light green: 60% or more completed.",
            "Dark green: 80% or more completed.",
            "Yellow: a mixed result where none of the thresholds above apply.",
        ),
        "When more than one could apply, the first match in this order wins: no scheduled tasks, 80%+ "
        "uncompleted, 80%+ completed, 60%+ uncompleted, 60%+ completed, 50%+ pending, otherwise yellow.",
        "Other cues: today's date is marked in the accent colour, the selected day has an accent outline, and a "
        "day whose saved schedule is out of date says Out of date in amber.",
    )),
)


def section(key: str) -> GuideSection:
    return next(item for item in GUIDE_SECTIONS if item.key == key)
