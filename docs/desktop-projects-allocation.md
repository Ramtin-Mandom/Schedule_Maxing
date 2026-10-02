# Projects and Allocation Planning

The sidebar now opens real native pages backed by the current workspace's
`PlanningController`. These pages need neither a local web server nor a cloud connection.

Project Schedule creates projects and edits their names/descriptions. It displays
saved placements with exact times and links to each date. Duplicate project names have
distinct suffixes and choices retain their IDs. Assign, reassign or clear a task's project in the shared
Day/Week/Month task form. The Projects page also offers a confirmed bulk move to another
project or no project. Writes use the versions read; deletion refuses live task
references and never cascades. Archive is not supported. Allocation materializes a range's recurring
occurrences first ([recurrence.md](recurrence.md)).

Allocation Planning previews a Monday-first week or real calendar month. Allocate /
Recalculate reads persisted tasks, dependencies, fixed blocks and preferences. It shows
assigned dates, remaining capacity and the service's reasons for tasks without a date.
Only a service-proven result is described as impossible. A preview is derived, not a
saved detailed schedule; reopen and recalculate it as needed.

Schedule this date generates only that date and checks the preview fingerprint before
committing. Open Day carries the range and fingerprint into Day's Make Schedule and
Regenerate actions. If inputs changed, return to Allocation Planning and recalculate.
Opening a date itself never schedules anything or changes a task's required date.
Back returns to the allocation page with its selected range and preview.

Project filters in Week, Month and Allocation change presentation only. Fixed blocks
remain visible on calendars, and all projects' dependencies and constraints remain in
the scheduling inputs. Freshness is re-read on return and after remote sync.

Verification lives in `tests/ui/test_projects_allocation.py` (real SQLite services)
and `tests/ui/test_planning_pages.py` (native controls and Day navigation).
