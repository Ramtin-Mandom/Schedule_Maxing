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
            "Points (top right): Completed / Possible points of the date -- its scheduled tasks, fixed blocks "
            "and To Dos, and its tasks that are not scheduled yet (possible only).",
            "Timeline: the whole day, 12 AM to 12 AM, fitted to the window (no sideways scrolling): fixed blocks "
            "in their category colour, scheduled tasks at their exact minutes, and the free gaps between them. "
            "Narrow blocks show their name vertically; select a block to read its details below.",
            "To Do: the date's sticky notes, directly under the timeline -- eight per row, two rows, more scroll "
            "down.",
            "Available tasks: tasks for this date (and tasks with no date) that are not on the schedule yet, "
            "with the reason when it is known -- up to four per row and two rows, more scroll down. Select one "
            "to edit it; its X removes it.",
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
    GuideSection("projects", "Projects", (
        "Project Schedule groups tasks that belong together, such as a course or a piece of work. The page opens "
        "on the overview: a form to create a project, then your projects in two lists.",
        (
            "New project: a name, and optionally a description, a start date and an estimated end date. Dates are "
            "typed as YYYY-MM-DD (for example 2026-09-23); the format is shown under each date field. Choose "
            "Create Project.",
            "Ongoing projects and Completed projects: click a bar to collapse or expand that list; each works on "
            "its own.",
            "Double-click a project (or choose Open) to open it. All projects takes you back to the overview.",
            "Inside a project: Mark complete moves it to Completed projects (Reopen project brings it back), "
            "Edit... changes its name, description, dates and task defaults, and Delete empty project removes a "
            "project that has no tasks left. Tasks are never deleted with a project.",
            "View performance opens the Productivity page on this project's statistics (see Productivity).",
        ),
        "On the Day, Week and Month pages a task of a project shows the first three letters of the project's name "
        "after its own, like Homework (mat) for a project named math. It is only shown that way: the task's name "
        "is not changed, and renaming the project changes the letters everywhere.",
    )),
    GuideSection("project_tasks", "Project tasks and milestones", (
        "An open project has three parts side by side: Add Task, Project Tasks and Milestones. The two lists "
        "scroll on their own when they get long.",
        (
            "Add Task is the same form as on the Day page, with a Date where the Day page has the project choice: "
            "the task joins this project on the date you type. It is added without a time -- open that day and "
            "Make Schedule to give it one.",
            "Project Tasks lists every task of the project, including ones you assigned to it from the Day, Week "
            "or Month form, with its date, its scheduled time (or Not scheduled) and whether it is completed.",
            "Done: tick it to complete a task right there, even one that has not been scheduled. This works for "
            "project tasks only; any other task is completed on its scheduled slot on the Day page. A completed "
            "task turns green, appears in the Completed column of the Day page for the day you completed it, and "
            "its points count in Productivity -- once, even if it is also scheduled. Untick Done to undo it.",
            "Click a task that is not completed to choose a day for it: the same task moves to that day without a "
            "time, and any time it was scheduled at is removed. Cancel changes nothing.",
            "× on the left of a task removes it after a confirmation, from the project and from the Day, Week and "
            "Month pages; points it already earned stay in your statistics.",
            "Task defaults (in Edit...): a default duration and points for this project's new tasks. "
            "Leave one empty to use the category's default. What you type in the form always wins, then the "
            "project's defaults, then the category's, then the app's. Changing them only affects tasks you add "
            "afterwards, and every new project starts with none set.",
        ),
        "Milestones are the project's checkpoints.",
        (
            "Add Milestone asks for a number (a whole number; milestones are listed in that order), a title and a "
            "description.",
            "Score: choose 1 to 10 on the right of a milestone; a new one starts at 1. The whole milestone is "
            "coloured by its score: 1-3 neutral, 4-7 light green, 8-9 green, 10 dark green.",
            "× on the left of a milestone removes it after a confirmation.",
        ),
    )),
    GuideSection("adding_tasks", "Adding tasks", (
        "The Add a task form is the same on Day, Week and Month. It has no date field: a new task always belongs "
        "to the date the page has selected. On the Day page that is the date shown (today unless you opened "
        "another); on Week and Month it is the day you selected. The form shows that date above the fields.",
        (
            "Task type: Flexible (the scheduler places it), Fixed (you set its start and end) or To Do (a "
            "checklist item that is never scheduled). The form shows only the fields of the type you choose.",
            "For a flexible task, name, category, duration (like 45 min or 1 h 15 min) and a preferred time are "
            "all you need.",
            "Points (0 to 1000, default 1) is what finishing it is worth to you, for every type; the - and + "
            "buttons change it by 10. It is counted in your productivity statistics when you complete it and "
            "never changes where the scheduler puts a task.",
            "Category and Project sit side by side. Without a category the task is saved as other. Project starts "
            "at None and lists your projects; a task with a project also appears in that project's task list.",
            "Times are entered as [hour] : [minute] [AM/PM]. Type the hour (1-12) and minute (00-59); click the "
            "AM/PM button, or type a or p, to switch. An end time of 12:00 AM means midnight at the end of the day.",
            "Required: the task must be scheduled; if it cannot be, the schedule is not saved and you are told why.",
            "The form has no advanced settings. A task that already has a deadline, dependencies, a task type "
            "or a repeat rule (an older or imported one) keeps them when you edit it.",
            "Removing a task, a fixed block or a To Do removes everything recorded for it too: its scheduled "
            "entries, its completion and its points.",
            "Use default values fills in the name (if empty), duration and points of the chosen "
            "category, or the general ones without a category. Change them, and add your own categories, in "
            "Settings > Task defaults and categories.",
            "Editing a task keeps its own date. Select a task on the timeline or in Available tasks to open it "
            "in the form; while it is open, Remove deletes it.",
        ),
    )),
    GuideSection("fixed_blocks", "Fixed tasks (blocks)", (
        "A fixed block is something that happens at a set time: a class, a shift, a meal, sleep. Choose Fixed "
        "in the form, then give it a label, a category, points and its start and end. The scheduler never moves "
        "a fixed block and never places anything on top of it.",
        (
            "A fixed block is on the Day page's Uncompleted | Tasks | Completed board like scheduled tasks: "
            "mark it completed to get its points. Its points never affect the schedule.",
            "Fixed blocks saved before they had points are worth 0 points and count as completed.",
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
        "Every flexible task has one preferred time: Early, Mid or Late. The day's schedulable hours (its day "
        "window, not midnight to midnight) are divided into three equal parts; an 8:00 AM to 11:00 PM day is "
        "Early 8:00 AM - 1:00 PM, Mid 1:00 PM - 6:00 PM and Late 6:00 PM - 11:00 PM.",
        "It is a very strong preference: whenever the task fits in its part of the day it is placed there, and "
        "the other scores only choose where inside it. It is still not a rule: when that part of the day is "
        "full, the task goes as close to it as it fits instead of being left out.",
        "A category can have a preferred time too (Settings or Day Preferences); it only applies to tasks "
        "without an Early / Mid / Late choice, such as imported ones.",
    )),
    GuideSection("todos", "To Do items", (
        "A To Do is a checklist item: a name, a category and points -- no duration and no time. It belongs to "
        "the date it was added on, is never scheduled and never appears on the timeline. To Dos are the sticky "
        "notes under the Day page's timeline: eight per row, two rows, and more scroll down.",
        (
            "Tick Done to complete one: you get its points, and it appears under Completed for that day.",
            "Untick it to take the completion back.",
            "The small X removes it, with its completion and points. Select its name to edit it in the form.",
        ),
    )),
    GuideSection("categories", "Categories", (
        "Every task and fixed block has a category: study, work, class, exercise, sleep, food, event, "
        "entertainment, errand or other. Categories give items their colour on the schedule and can have their "
        "own importance (a multiplier on the scheduler's score; 1 is neutral) and preferred time in Settings.",
    )),
    GuideSection("priority_points", "The scheduler's score, and points", (
        "Tasks have no priority. When the scheduler compares possible times for a task, it first keeps to the "
        "task's preferred time (Early, Mid or Late) wherever that is possible, and then gives each remaining "
        "time a score for how good the placement is:",
        (
            "the category's importance (times the importance weight);",
            "a bonus for being inside, or close to, the preferred time;",
            "a bonus for being near tasks with the same or related tags;",
            "a penalty for leaving awkward small gaps (the fragmentation penalty).",
        ),
        "The highest-scoring valid time wins. The weights are in Settings under Default scheduling preferences.",
        "Points are different: they are your own value for a task (set in the form), counted when you complete "
        "it. The scheduler never reads them, and its score is never your points.",
    )),
    GuideSection("dependencies", "Dependencies", (
        "A task that depends on another (set in an imported file or an older version) is only "
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
            "A project task completed with Done in its project, without ever being scheduled, appears in "
            "Completed on the day you completed it. Its × brings it back to not completed.",
            "Completed and uncompleted work, with your points and planned versus actual times, appears on the "
            "Productivity page.",
        ),
    )),
    GuideSection("productivity", "Productivity", (
        "The Productivity page shows how your plans went. The bar at the top chooses one of three sections, and "
        "everything is recalculated from your saved records each time you open it.",
        (
            "General: your records (such as your highest-point day and best week) and your stats at a glance. It "
            "has no filters.",
            "Specific: two parts, each with its own filters. Task-based is filtered by period, category, tag and "
            "task type and shows each task type's completion. Time-based is filtered by date range, day of week "
            "and time of day and shows totals, weekdays, recent weeks and months, a single day and two charts.",
            "Project: your projects. Select one to see the points it collected: the total, the points for each "
            "day something was completed, and the average per day. Date range chooses All time or the last 7, 30 "
            "or 90 days.",
            "The average is the total divided by every calendar day of the period, including days when nothing "
            "was completed; the box says which period and how many days. All time starts on the day of the "
            "project's first completed task. A project with nothing completed shows 0 points and no average.",
            "View performance inside a project opens this Project section with that project already selected.",
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
        "Your data is saved on this computer first, so the app works without an account and without internet. "
        "That is guest mode: everything stays on this device, and is still there after a restart.",
        (
            "Create an account on the Account page (after entering the backend address) to keep your work in it: "
            "everything already on this device becomes that account's and is uploaded. Nothing is cleared or "
            "replaced.",
            "Signing in to an account you already have never changes work done without an account. The app asks "
            "whether to add it to the account or keep it separate; keeping it separate changes nothing.",
            "While you are signed in, saving is still local and instant. Changes are sent automatically within "
            "seconds, and changes from your other devices come back the same way, including each date's day "
            "window. You never need to press anything to save or upload.",
            "Offline, or after your session ends, you keep working in your account's records. Changes wait "
            "safely, even across a restart, and are sent once the backend is reachable and you are signed in.",
            "The status bar at the top shows guest mode or your account, online or offline, what is waiting and "
            "the last sync. Sync now retries at once; Check connection only checks the connection.",
            "Sign out hides that account's records on this device and returns to guest mode. Unsent changes are "
            "kept and sent when you sign in to it again; another account never receives them.",
            "If the same record changed in two places, the Account page shows the conflict and lets you choose. "
            "Nothing is overwritten silently.",
        ),
        "With direct PostgreSQL storage everything is saved straight to the database, so there is nothing to "
        "sync.",
    )),
    GuideSection("reset", "Reset", (
        "Reset Day, Reset Week and Reset Month clear the tasks planned for those dates, their fixed blocks, "
        "their To Dos, their scheduled entries, everything recorded for them (completions, points and work "
        "sessions) and their own preferences (including a custom day window). You always see exactly what "
        "would be deleted and confirm first.",
        (
            "Kept: tasks with no date, tasks planned on other dates, projects (with their milestones and task "
            "defaults), your default preferences and everything recorded on other dates.",
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
