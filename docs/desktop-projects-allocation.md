# Projects

The sidebar's Project Schedule page is a real native page backed by the current workspace's
`PlanningController`. It needs neither a local web server nor a cloud connection.

Project Schedule opens on an overview: a creation form (name, description, start date,
estimated end date) above two independently collapsible lists, ongoing and completed
projects. Double-clicking a project (or its Open button) opens its detail view; "All
projects" returns. There a project is marked complete or reopened, edited (name,
description, dates) or, when empty, deleted. Writes use the versions read; deletion refuses
live task references and never cascades. Archive is not supported. Duplicate project names
have distinct suffixes in choices, which retain their IDs. Assign, reassign or clear a
task's project in the shared Day/Week/Month task form (`ProjectsController.reassign_tasks`
moves a whole project's tasks; the page no longer offers it).

The detail view has three sections; Project Tasks and Milestones scroll on their own:

- **Add Task** is the shared task form with a date where the Day page's has the project
  choice (fixed blocks are not offered: they belong to no project). The task is saved for
  that date exactly as one added on a Week/Month day: no time slot, nothing is scheduled.
- **Project Tasks** lists every task of the project, wherever it was created, with its
  saved placements and its completion in words; a row is green only once its task is
  finished. Each row has an "×" on the left (the schedule pages' own removal and
  confirmation: saved placements go with the task, execution history is kept) and a
  **Done** checkbox. Clicking the row of an unfinished task asks for a day and moves that
  same task there unscheduled: its saved placements are removed (never-started attempts
  are withdrawn, started or finished history is kept).
- **Milestones** belong to the project: a number (any whole number; the list is ascending,
  equal numbers in the order added), a title, a description and a score from 1 to 10
  (new: 1). The whole widget is colored by its score's band -- 1-3 neutral, 4-7 light
  green, 8-9 green, 10 dark green -- and has an "×" on the left that removes it after a
  confirmation.

**Completing a project's task** needs no time slot. This is the one exception to "a task
is completed on its scheduled slot", and it applies only to tasks that belong to a project
(`ProjectsController.set_task_completed`). A scheduled task is completed on its slot, as
its card on the Day page does. An unscheduled one gets a task-only completion record
(`app/execution/direct_completion.py`: an execution of the task without a placement --
nothing is scheduled to allow it), and the Day page (and the Week/Month day panel) lists
it in Completed on the date it was completed. Either way it is one completion in the
ordinary history: the same points, statistics and synchronization as any other. Repeating
the action adds nothing, unticking Done (or "back to Tasks" on the Day board) reopens the
record and withdraws it from every statistic, and if a task ends up completed both on a
slot and directly the analytics count it once (`tracker.merge_completions`).

**Project task defaults.** A project may configure the duration, priority and points of
its new tasks (Edit…; `Project.task_defaults`, each value unset by default and never
inherited from another project). For a new task of the project the precedence is: what
was typed, then the project's configured values, then the category's defaults, then the
application's (`app/ui/task_defaults.resolve_task_default`); an unset project value never
replaces a category default. The Add Task form starts from the configured values and its
"Use default values" fills in the resolved ones. Changing the defaults affects tasks added
afterwards only.

**View performance** opens Performance -> Project with the project selected
(docs/analytics.md).

The dates, completion and milestones are stored with the project (SQLite schemas 14-15,
backend migration 0014) and synchronize with a server that has the `project_details`
feature; an older server or client never erases them.

Schedule views (Day, Week, Month) show a project task's name with the project's
abbreviation -- the first three characters of its trimmed name -- as in "Homework (mat)".
It is display text built from the project's current name; the stored task name is unchanged.

Opening a date from a project never schedules anything or changes a task's required date;
Day's Make Schedule and Regenerate actions do that. Week/month date allocation remains part
of the shared planning workflow (generation, the CLI and the web API); the desktop has no
separate Allocation Planning page.

Project filters in Week and Month change presentation only. Fixed blocks
remain visible on calendars, and all projects' dependencies and constraints remain in
the scheduling inputs. Freshness is re-read on return and after remote sync.

Verification lives in `tests/ui/test_projects_allocation.py` and
`tests/ui/test_project_details.py` (real SQLite services)
and `tests/ui/test_planning_pages.py` (native controls and Day navigation).
