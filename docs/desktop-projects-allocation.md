# Projects

The sidebar's Project Schedule page is a real native page backed by the current workspace's
`PlanningController`. It needs neither a local web server nor a cloud connection.

Project Schedule creates projects and edits their names/descriptions. It displays
saved placements with exact times and links to each date. Duplicate project names have
distinct suffixes and choices retain their IDs. Assign, reassign or clear a task's project in the shared
Day/Week/Month task form. The Projects page also offers a confirmed bulk move to another
project or no project. Writes use the versions read; deletion refuses live task
references and never cascades. Archive is not supported.

Opening a date from a project never schedules anything or changes a task's required date;
Day's Make Schedule and Regenerate actions do that. Week/month date allocation remains part
of the shared planning workflow (generation, the CLI and the web API); the desktop has no
separate Allocation Planning page.

Project filters in Week and Month change presentation only. Fixed blocks
remain visible on calendars, and all projects' dependencies and constraints remain in
the scheduling inputs. Freshness is re-read on return and after remote sync.

Verification lives in `tests/ui/test_projects_allocation.py` (real SQLite services)
and `tests/ui/test_planning_pages.py` (native controls and Day navigation).
