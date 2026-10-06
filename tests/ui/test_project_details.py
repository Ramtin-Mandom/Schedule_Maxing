"""
The Projects page's presenter beyond CRUD (app/ui/projects_controller.py): planned dates and completion,
milestones, a project's tasks (added and moved unscheduled, completion read from executions) and the
project abbreviation schedule views show -- all on real SQLite services.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from app.execution.db import LATEST_SCHEMA_VERSION, initialize_schema
from app.planning.models import Project, ProjectMilestone, Task, project_abbreviation, task_display_name
from app.planning.time import local_date_of
from app.ui.app_services import open_app_services
from app.ui.calendar_controller import CalendarController
from app.ui.day_controller import DayScheduleController
from app.ui.day_outcomes import DayOutcomeController
from app.execution.lifecycle import TaskOutcome
from app.ui.projects_controller import ProjectsController, milestone_score_band
from app.ui.task_defaults import TaskDefault, resolve_task_default
from app.ui.task_status import TaskStatusController
from app.ui.task_form_model import FormErrors, TaskDraft
from tests.ui.test_day_controller import db_path as db_path, ok, services as services  # noqa: F401

DAY = date(2026, 9, 24)
LATER = date(2026, 9, 28)


def controller(services) -> ProjectsController:
    return ProjectsController(services.planning_controller, timezone="UTC")


def task(planning, name, project=None, **kwargs):
    return ok(planning.add_or_update_task(Task(**{
        "name": name, "category": "study", "estimated_duration_minutes": 13, "priority": 5, "project_id": project,
        **kwargs})))


def draft(name="Read chapter", **kwargs) -> TaskDraft:
    return TaskDraft(**{"kind": "task", "name": name, "category": "study", "duration": "30m", "priority": "5",
                        **kwargs})


# ---------------------------------------------------------------------------------------------- project fields


def test_a_project_is_created_with_its_dates_and_the_dates_are_validated(services):
    projects = controller(services)
    created = ok(projects.create("Thesis", "Write it", "2026-09-01", "2026-12-15"))
    assert (created.start_date, created.estimated_end_date) == (date(2026, 9, 1), date(2026, 12, 15))
    row = ok(projects.load()).projects[0]
    assert row.dates_text and not row.completed
    assert ok(projects.create("No dates")).start_date is None  # both dates are optional

    assert not projects.create("", "x").ok
    wrong = projects.create("Bad", "", "next week", "")
    assert not wrong.ok and "Start date" in wrong.error
    backwards = projects.create("Backwards", "", "2026-10-02", "2026-10-01")
    assert not backwards.ok and "before the start date" in backwards.error
    assert [p.name for p in ok(projects.load()).projects] == ["No dates", "Thesis"]  # nothing refused was saved


def test_completing_and_reopening_moves_a_project_between_the_two_lists(services):
    projects = controller(services)
    project = ok(projects.create("Ship"))
    kept = task(services.planning_controller, "Keep me", project.id)
    done = ok(projects.set_completed(project.id, True, expected_version=project.version))
    assert done.is_completed and done.version == project.version + 1
    snapshot = ok(projects.load(project.id))
    assert [row.name for row in snapshot.completed] == ["Ship"] and snapshot.ongoing == []
    assert [row.task_id for row in snapshot.tasks] == [kept.id]  # its tasks are untouched
    assert not projects.set_completed(project.id, False, expected_version=project.version).ok  # a stale version
    reopened = ok(projects.set_completed(project.id, False, expected_version=done.version))
    assert reopened.completed_at is None
    assert [row.name for row in ok(projects.load()).ongoing] == ["Ship"]


def test_an_edit_keeps_the_details_it_does_not_change(services):
    projects = controller(services)
    project = ok(projects.create("Plan", "", "2026-09-01", "2026-09-30"))
    ok(projects.add_milestone(project.id, "1", "Outline", "The outline is agreed"))
    current = ok(services.planning_controller.get_project(project.id))
    renamed = ok(projects.update(project.id, "Plan B", "Now described", expected_version=current.version))
    assert renamed.start_date == date(2026, 9, 1) and len(renamed.milestones) == 1  # a rename keeps them
    cleared = ok(projects.update(project.id, "Plan B", "", expected_version=renamed.version, start_date="",
                                 estimated_end_date="2026-10-05", set_dates=True))
    assert (cleared.start_date, cleared.estimated_end_date) == (None, date(2026, 10, 5))


# ---------------------------------------------------------------------------------------------- milestones


def test_milestones_are_validated_ordered_by_number_and_scored(services):
    projects = controller(services)
    project = ok(projects.create("Course"))
    for number, title in (("3", "Third"), ("1", "First"), ("3", "Also third"), ("-2", "Before")):
        ok(projects.add_milestone(project.id, number, title, f"{title} is done when reviewed"))
    snapshot = ok(projects.load(project.id))
    # Ascending by number; the two numbered 3 keep the order they were added in.
    assert [(m.number, m.title) for m in snapshot.milestones] == [
        (-2, "Before"), (1, "First"), (3, "Third"), (3, "Also third")]
    assert {m.score for m in snapshot.milestones} == {1}  # the initial score

    for number, title, description in (("", "T", "D"), ("1.5", "T", "D"), ("two", "T", "D"), ("2", " ", "D"),
                                       ("2", "T", "  ")):
        assert not projects.add_milestone(project.id, number, title, description).ok
    assert len(ok(projects.load(project.id)).milestones) == 4

    target = snapshot.milestones[1]
    ok(projects.set_milestone_score(project.id, target.id, 8))
    assert {m.title: m.score for m in ok(projects.load(project.id)).milestones}["First"] == 8
    for score in (0, 11, True, "5"):
        assert not projects.set_milestone_score(project.id, target.id, score).ok
    assert not projects.set_milestone_score(project.id, uuid.uuid4(), 5).ok
    assert {m.title: m.score for m in ok(projects.load(project.id)).milestones}["First"] == 8


@pytest.mark.parametrize("score, band", [(1, "neutral"), (3, "neutral"), (4, "light"), (7, "light"), (8, "green"),
                                         (9, "green"), (10, "dark")])
def test_score_color_bands(score, band):
    assert milestone_score_band(score) == band


def test_a_milestone_model_refuses_scores_outside_one_to_ten():
    assert ProjectMilestone(number=1, title="T").score == 1
    for score in (0, 11):
        with pytest.raises(ValueError):
            ProjectMilestone(number=1, title="T", score=score)
    with pytest.raises(ValueError):
        Project(name="P", start_date=date(2026, 2, 2), estimated_end_date=date(2026, 2, 1))


def test_project_details_survive_a_reopen(tmp_path):
    path = tmp_path / "details.db"
    opened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        projects = ProjectsController(opened.planning_controller, timezone="UTC")
        project = ok(projects.create("Durable", "Kept", "2026-09-01", "2026-09-30"))
        ok(projects.add_milestone(project.id, "2", "Second", "A longer description\nover two lines"))
        ok(projects.add_milestone(project.id, "1", "First", "Starts it"))
        first = ok(projects.load(project.id)).milestones[0]
        ok(projects.set_milestone_score(project.id, first.id, 10))
        current = ok(opened.planning_controller.get_project(project.id))
        ok(projects.set_completed(project.id, True, expected_version=current.version))
    finally:
        opened.close()
    reopened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        stored = ok(reopened.planning_controller.get_project(project.id))
        assert (stored.start_date, stored.estimated_end_date) == (date(2026, 9, 1), date(2026, 9, 30))
        assert stored.is_completed
        view = ok(ProjectsController(reopened.planning_controller, timezone="UTC").load(project.id))
        assert [(m.number, m.title, m.description, m.score, m.band) for m in view.milestones] == [
            (1, "First", "Starts it", 10, "dark"), (2, "Second", "A longer description\nover two lines", 1, "neutral")]
    finally:
        reopened.close()


def test_a_project_stored_before_the_details_existed_loads_with_defaults(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path), isolation_level=None)
    initialize_schema(raw, target_version=13)
    assert "milestones" not in {row[1] for row in raw.execute("PRAGMA table_info(projects)")}
    project_id, stamp = str(uuid.uuid4()), datetime(2026, 1, 5, tzinfo=timezone.utc).isoformat()
    raw.execute("INSERT INTO projects (id, user_id, name, description, created_at, updated_at, version) "
                "VALUES (?, NULL, 'Old project', NULL, ?, ?, 4)", (project_id, stamp, stamp))
    raw.close()
    opened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        stored = ok(opened.planning_controller.get_project(uuid.UUID(project_id)))
        assert (stored.name, stored.version) == ("Old project", 4)
        assert (stored.start_date, stored.estimated_end_date, stored.completed_at, stored.milestones) == (
            None, None, None, [])
        row = ok(ProjectsController(opened.planning_controller, timezone="UTC").load()).ongoing[0]
        assert row.name == "Old project" and row.dates_text == ""
    finally:
        opened.close()
    check = sqlite3.connect(str(path))
    assert check.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 14
    check.close()


# ---------------------------------------------------------------------------------------------- the project's tasks


def test_a_task_added_in_a_project_is_dated_unscheduled_and_listed_at_once(services):
    planning = services.planning_controller
    projects = controller(services)
    project = ok(projects.create("math"))
    other = ok(projects.create("Other"))
    assert projects.add_task(project.id, draft(project_id=other.id), DAY.isoformat()).ok  # the open project wins

    rows = ok(projects.load(project.id)).tasks
    assert [(row.name, row.status, row.dates, row.completed) for row in rows] == [
        ("Read chapter", "Not scheduled", (DAY,), False)]
    stored = ok(planning.get_task(rows[0].task_id))
    assert stored.project_id == project.id and stored.preferred_dates == [DAY] and stored.required_date is None
    assert ok(planning.get_placements(DAY)) == []  # no time slot, nothing was scheduled
    # It waits on that date exactly as a task added on the Week page's selected day does.
    week = ok(CalendarController(planning, mode="week", selected=DAY, timezone="UTC").load())
    assert [(item.kind, item.name) for item in week.day(DAY).items] == [("unscheduled", "Read chapter (mat)")]


def test_the_project_task_form_keeps_the_task_forms_validation_and_needs_a_date(services):
    projects = controller(services)
    project = ok(projects.create("Checks"))
    no_date = projects.add_task(project.id, draft(), "")
    assert not no_date.ok and isinstance(no_date.cause, FormErrors) and "date" in no_date.cause.errors
    assert not projects.add_task(project.id, draft(), "soon").ok
    invalid = projects.add_task(project.id, draft(name=" ", duration="never"), DAY.isoformat())
    assert not invalid.ok and {"name", "duration"} <= set(invalid.cause.errors)
    assert ok(projects.load(project.id)).tasks == []


def test_tasks_created_elsewhere_are_listed_with_their_completion(services):
    planning = services.planning_controller
    projects = controller(services)
    project = ok(projects.create("Mixed"))
    finished = task(planning, "Finished", project.id, required_date=DAY)
    waiting = task(planning, "Waiting", project.id, preferred_dates=[LATER])
    task(planning, "Not in it", None, required_date=DAY)
    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    outcomes = DayOutcomeController(planning, services.execution_controller)
    ok(outcomes.set_day(DAY, "completed"))

    rows = {row.name: row for row in ok(projects.load(project.id)).tasks}
    assert set(rows) == {"Finished", "Waiting"}
    assert rows["Finished"].completed and rows["Finished"].status_text == "✓ Completed"
    assert not rows["Finished"].movable  # only an incomplete task can be given a day
    assert not rows["Waiting"].completed and rows["Waiting"].status_text == "○ Not completed"
    assert rows["Waiting"].movable and rows["Waiting"].task_id == waiting.id
    # The same completion the schedule views show: undoing it on the Day side is seen here.
    ok(outcomes.set_day(DAY, "uncompleted"))
    assert not {row.name: row for row in ok(projects.load(project.id)).tasks}["Finished"].completed
    assert finished.id == rows["Finished"].task_id


def test_a_task_is_finished_and_reopened_from_its_project_on_its_time_slot(services):
    planning = services.planning_controller
    projects = ProjectsController(planning, timezone="UTC", executions=services.execution_controller)
    project = ok(projects.create("Finish"))
    ok(projects.add_task(project.id, draft("Fresh"), DAY.isoformat()))
    fresh = ok(projects.load(project.id)).tasks[0]
    assert not fresh.completed and not fresh.scheduled  # a task just added is never finished

    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    assert ok(projects.load(project.id)).tasks[0].scheduled
    assert ok(projects.set_task_completed(fresh.task_id, True)) is True
    assert ok(projects.load(project.id)).tasks[0].completed
    board = ok(DayOutcomeController(planning, services.execution_controller).detail(DAY)).board
    assert [card.outcome.value for card in board.cards] == ["completed"]  # the Day page shows the same
    assert ok(projects.set_task_completed(fresh.task_id, True)) is True  # repeating it changes nothing
    assert len(services.execution_controller.executions_for_placements(
        [p.id for p in ok(planning.get_placements(DAY))]).value) == 1

    assert ok(projects.set_task_completed(fresh.task_id, False)) is False
    assert not ok(projects.load(project.id)).tasks[0].completed
    assert not controller(services).set_task_completed(fresh.task_id, True).ok  # read-only without executions


def full(services) -> ProjectsController:
    return ProjectsController(services.planning_controller, timezone="UTC", executions=services.execution_controller)


def direct_executions(services, task_id):
    return [e for e in services.execution_controller._service.list_executions()
            if e.task_id == task_id and e.scheduled_task_id is None]


def test_an_unscheduled_project_task_is_completed_and_undone_without_a_time_slot(services):
    planning, executions = services.planning_controller, services.execution_controller
    projects = full(services)
    project = ok(projects.create("math"))
    ok(projects.add_task(project.id, draft("Essay", points="8"), DAY.isoformat()))
    essay = ok(projects.load(project.id)).tasks[0]
    assert not essay.scheduled and not essay.completed

    assert ok(projects.set_task_completed(essay.task_id, True)) is True
    assert ok(projects.load(project.id)).tasks[0].completed
    [execution] = direct_executions(services, essay.task_id)
    assert execution.status.value == "completed" and execution.points == 8 and execution.scheduled_task_id is None
    assert ok(planning.placements_for_tasks([essay.task_id]))[essay.task_id] == []  # no session was invented
    completed_on = execution.actual_final_end_at.date()

    # It is in the Completed column of the day it was completed (not of its planned date), on Day and Week/Month.
    snapshot = ok(DayScheduleController(planning, anchor_date=completed_on, timezone="UTC").load())
    assert [item.display_name for item in snapshot.direct_completions] == ["Essay (mat)"]
    board = ok(TaskStatusController(executions).board(completed_on, snapshot.executables, snapshot.direct_completions))
    [card] = board.column(TaskOutcome.COMPLETED)
    assert card.direct and card.name == "Essay (mat)" and card.key == uuid.UUID(execution.id)
    panel = ok(DayOutcomeController(planning, executions, timezone="UTC").detail(completed_on))
    assert [c.name for c in panel.board.column(TaskOutcome.COMPLETED)] == ["Essay (mat)"]
    if completed_on != DAY:
        assert ok(DayScheduleController(planning, anchor_date=DAY, timezone="UTC").load()).direct_completions == []

    # The ordinary analytics count it: its points, once, on that day.
    productivity = services.productivity_controller
    report = ok(productivity.build_project_points(project.id))
    assert (report.total_points, report.completed_count) == (8, 1)
    # The report's own day is in the reporting time zone (the services'), whatever zone a page shows.
    reported_on = local_date_of(execution.actual_final_end_at, services.timezone)
    assert [(day.date, day.points) for day in report.by_day] == [(reported_on, 8)]
    assert ok(productivity.build_tracker()).general.activity.known_points == 8

    # Repeating the action adds no second completion and no second set of points.
    assert ok(projects.set_task_completed(essay.task_id, True)) is True
    assert len(direct_executions(services, essay.task_id)) == 1
    assert ok(productivity.build_project_points(project.id)).total_points == 8

    # Undo withdraws the completion and its statistics; completing again reuses the same record.
    assert ok(projects.set_task_completed(essay.task_id, False)) is False
    assert not ok(projects.load(project.id)).tasks[0].completed
    assert ok(productivity.build_project_points(project.id)).total_points == 0
    assert ok(DayScheduleController(planning, anchor_date=completed_on, timezone="UTC").load()).direct_completions == []
    assert ok(projects.set_task_completed(essay.task_id, False)) is False  # nothing left to undo: no change
    assert ok(projects.set_task_completed(essay.task_id, True)) is True
    assert [e.id for e in direct_executions(services, essay.task_id)] == [execution.id]
    assert ok(productivity.build_project_points(project.id)).total_points == 8

    # The Day board's own "back to Tasks" undoes it too; no other move applies to a task without a slot.
    snapshot = ok(DayScheduleController(planning, anchor_date=completed_on, timezone="UTC").load())
    status = TaskStatusController(executions)
    [card] = ok(status.board(completed_on, snapshot.executables, snapshot.direct_completions)).cards
    assert not status.move(card, TaskOutcome.UNCOMPLETED).ok
    assert status.move(card, TaskOutcome.PENDING).ok
    assert not ok(projects.load(project.id)).tasks[0].completed


def test_a_scheduled_project_task_completed_from_the_project_is_never_counted_twice(services):
    planning = services.planning_controller
    projects = full(services)
    project = ok(projects.create("Once"))
    ok(projects.add_task(project.id, draft("Report", points="5"), DAY.isoformat()))
    report_task = ok(projects.load(project.id)).tasks[0]
    ok(projects.set_task_completed(report_task.task_id, True))  # directly, before it was ever scheduled
    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    if ok(planning.placements_for_tasks([report_task.task_id]))[report_task.task_id]:
        # It was scheduled afterwards and is completed there as well: still one completion, five points.
        ok(DayOutcomeController(planning, services.execution_controller).set_day(DAY, "completed"))
    points = ok(services.productivity_controller.build_project_points(project.id))
    assert (points.total_points, points.completed_count) == (5, 1)
    # Undoing from the project withdraws it everywhere.
    assert ok(projects.set_task_completed(report_task.task_id, False)) is False
    assert not ok(projects.load(project.id)).tasks[0].completed
    assert ok(services.productivity_controller.build_project_points(project.id)).total_points == 0


def test_a_task_outside_a_project_still_needs_a_time_slot_to_be_completed(services):
    planning = services.planning_controller
    projects = full(services)
    loose = task(planning, "Loose", None, required_date=DAY)
    refused = projects.set_task_completed(loose.id, True)
    assert not refused.ok and "scheduled slot" in refused.error
    assert direct_executions(services, loose.id) == []


def test_project_task_defaults_precedence_and_persistence(tmp_path):
    path = tmp_path / "defaults.db"
    opened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        projects = ProjectsController(opened.planning_controller, timezone="UTC")
        project = ok(projects.create("Configured"))
        other = ok(projects.create("Untouched"))
        assert not project.task_defaults.configured  # a new project configures nothing
        category = TaskDefault("Study", duration=50, priority=6, points=30)
        # Unset project values never replace the category's (or the application's) defaults.
        assert resolve_task_default(category, project.task_defaults) == category
        assert resolve_task_default(TaskDefault("Task"), None) == TaskDefault("Task", 60, 5, 20)

        for bad in (("soon", "", ""), ("", "11", ""), ("", "", "-3")):
            assert not projects.update(project.id, "Configured", "", expected_version=project.version,
                                       task_defaults=bad).ok
        saved = ok(projects.update(project.id, "Configured", "", expected_version=project.version,
                                   task_defaults=("45m", "", "12")))
        assert saved.task_defaults.model_dump() == {"duration_minutes": 45, "priority": None, "points": 12}
        # The project's configured values win; the unset priority still comes from the category.
        assert resolve_task_default(category, saved.task_defaults) == TaskDefault("Study", 45, 6, 12)
        assert not ok(opened.planning_controller.get_project(other.id)).task_defaults.configured  # not inherited

        # A new task with nothing typed for the duration takes the project's; a typed value always wins.
        ok(projects.add_task(project.id, draft("Defaulted", duration=""), DAY.isoformat()))
        ok(projects.add_task(project.id, draft("Explicit", duration="20m"), DAY.isoformat()))
        assert not projects.add_task(other.id, draft("No default", duration=""), DAY.isoformat()).ok
        durations = {t.name: t.estimated_duration_minutes for t in ok(opened.planning_controller.list_tasks())}
        assert durations == {"Defaulted": 45, "Explicit": 20}

        # Changing the defaults affects future tasks only.
        current = ok(opened.planning_controller.get_project(project.id))
        ok(projects.update(project.id, "Configured", "", expected_version=current.version,
                           task_defaults=("1h 30m", "9", "")))
        ok(projects.add_task(project.id, draft("Later", duration=""), DAY.isoformat()))
        durations = {t.name: t.estimated_duration_minutes for t in ok(opened.planning_controller.list_tasks())}
        assert durations == {"Defaulted": 45, "Explicit": 20, "Later": 90}
    finally:
        opened.close()
    reopened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        stored = ok(reopened.planning_controller.get_project(project.id))
        assert stored.task_defaults.model_dump() == {"duration_minutes": 90, "priority": 9, "points": None}
        row = ok(ProjectsController(reopened.planning_controller, timezone="UTC").load(project.id)).selected
        assert row.task_defaults == stored.task_defaults
    finally:
        reopened.close()


def test_removing_a_task_and_a_milestone_from_the_project(services):
    planning = services.planning_controller
    projects = full(services)
    project = ok(projects.create("Trim"))
    kept = task(planning, "Kept", project.id, required_date=DAY)
    gone = task(planning, "Gone", project.id, required_date=DAY)
    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    description, choices = ok(projects.task_removal(gone.id))
    assert "Gone" in description and "schedule entries" in description and choices == []  # the schedule pages' words
    row = next(row for row in ok(projects.load(project.id)).tasks if row.task_id == gone.id)
    assert not projects.remove_task(gone.id, expected_version=row.version + 1).ok  # a stale version removes nothing
    assert projects.remove_task(gone.id, expected_version=row.version).ok
    assert [row.task_id for row in ok(projects.load(project.id)).tasks] == [kept.id]
    assert [p.task_id for p in ok(planning.get_placements(DAY))] == [kept.id]  # its placement went with it
    assert not projects.task_removal(gone.id).ok

    for number, title in (("1", "First"), ("2", "Second")):
        ok(projects.add_milestone(project.id, number, title, f"{title} done"))
    first, second = ok(projects.load(project.id)).milestones
    ok(projects.set_milestone_score(project.id, second.id, 9))
    ok(projects.remove_milestone(project.id, first.id))
    assert [(m.title, m.score) for m in ok(projects.load(project.id)).milestones] == [("Second", 9)]
    assert not projects.remove_milestone(project.id, first.id).ok
    assert [row.task_id for row in ok(projects.load(project.id)).tasks] == [kept.id]  # nothing else was touched


def test_a_task_removed_from_its_project_is_gone_from_the_day_views_too(services):
    planning, executions = services.planning_controller, services.execution_controller
    projects = full(services)
    project = ok(projects.create("Gone"))
    ok(projects.add_task(project.id, draft("Direct"), DAY.isoformat()))
    ok(projects.add_task(project.id, draft("Slotted"), DAY.isoformat()))
    ok(projects.add_task(project.id, draft("Waiting"), LATER.isoformat()))
    rows = {row.name: row for row in ok(projects.load(project.id)).tasks}
    ok(projects.set_task_completed(rows["Direct"].task_id, True))  # completed without a time slot
    completed_on = direct_executions(services, rows["Direct"].task_id)[0].actual_final_end_at.date()
    for row in rows.values():
        if row.name != "Direct":  # keep Direct unscheduled: only its completion puts it on a day
            continue
        ok(planning.add_or_update_task(ok(planning.get_task(row.task_id)).model_copy(
            update={"preferred_dates": [], "required_date": None}), expected_version=row.version))
    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    ok(projects.set_task_completed(rows["Slotted"].task_id, True))  # completed on its slot

    def day_names(day):
        snapshot = ok(DayScheduleController(planning, anchor_date=day, timezone="UTC").load())
        board = ok(TaskStatusController(executions).board(day, snapshot.executables, snapshot.direct_completions))
        return ({card.name for card in board.cards}, {row.name for row in snapshot.rows},
                {item.name for item in snapshot.timeline} | {item.name for item in snapshot.unplaced})

    assert "Direct (Gon)" in day_names(completed_on)[0] and "Slotted (Gon)" in day_names(DAY)[0]
    assert "Waiting (Gon)" in day_names(LATER)[2]

    for row in ok(projects.load(project.id)).tasks:
        assert projects.remove_task(row.task_id, expected_version=row.version).ok
    assert ok(projects.load(project.id)).tasks == []
    for day in {completed_on, DAY, LATER}:
        board, listed, placed = day_names(day)
        assert not any("(Gon)" in name for name in board | listed | placed), (day, board, listed, placed)
    panel = ok(DayOutcomeController(planning, executions, timezone="UTC").detail(completed_on))
    assert not any("Direct" in card.name for card in panel.board.cards)
    week = ok(CalendarController(planning, mode="week", selected=DAY, timezone="UTC").load())
    assert not any("(Gon)" in item.name for cell in week.days for item in cell.items)


def test_giving_a_task_a_day_moves_the_same_task_there_without_its_time_slot(services):
    planning = services.planning_controller
    projects = controller(services)
    project = ok(projects.create("Move"))
    pinned = task(planning, "Pinned", project.id, required_date=DAY)
    neighbour = task(planning, "Neighbour", None, required_date=DAY)
    assert DayScheduleController(planning, anchor_date=DAY, timezone="UTC").make_schedule_for(DAY).ok
    assert {p.task_id for p in ok(planning.get_placements(DAY))} == {pinned.id, neighbour.id}
    row = ok(projects.load(project.id)).tasks[0]
    assert row.dates == (DAY,) and row.status != "Not scheduled"

    moved = ok(projects.assign_date(row.task_id, LATER, expected_version=row.version))
    assert moved.id == pinned.id and moved.required_date == LATER  # the same task; it stays pinned to its date
    assert len(ok(planning.list_tasks())) == 2  # nothing was duplicated
    assert [p.task_id for p in ok(planning.get_placements(DAY))] == [neighbour.id]  # only its own slot went
    assert ok(planning.get_placements(LATER)) == []
    after = ok(projects.load(project.id)).tasks[0]
    assert (after.task_id, after.status, after.dates) == (pinned.id, "Not scheduled", (LATER,))
    week = CalendarController(planning, mode="week", selected=LATER, timezone="UTC")
    assert [(item.kind, item.name) for item in ok(week.load()).day(LATER).items] == [("unscheduled", "Pinned (Mov)")]
    earlier = ok(CalendarController(planning, mode="week", selected=DAY, timezone="UTC").load())
    assert [item.ref.id for item in earlier.day(DAY).items] == [neighbour.id]

    # The version the row was drawn with is the precondition: a stale one changes nothing.
    stale = projects.assign_date(row.task_id, DAY, expected_version=row.version)
    assert not stale.ok and ok(planning.get_task(pinned.id)).required_date == LATER

    flexible = task(planning, "Flexible", project.id, preferred_dates=[DAY, date(2026, 9, 20)])
    again = ok(projects.assign_date(flexible.id, LATER, expected_version=flexible.version))
    assert again.required_date is None and min(again.preferred_dates) == LATER


# ---------------------------------------------------------------------------------------------- abbreviations


@pytest.mark.parametrize("name, abbreviation", [("math", "mat"), ("AP", "AP"), ("  Chemistry ", "Che"), ("x", "x"),
                                                ("", ""), (None, "")])
def test_project_abbreviation(name, abbreviation):
    assert project_abbreviation(name) == abbreviation


def test_display_names_carry_the_abbreviation_once_and_only_for_project_tasks():
    assert task_display_name("Homework", "math") == "Homework (mat)"
    assert task_display_name("Essay", "AP") == "Essay (AP)"
    assert task_display_name("Laundry", None) == "Laundry"
    assert task_display_name("Laundry", "   ") == "Laundry"


def test_schedule_views_show_the_abbreviation_and_follow_a_rename(services):
    planning = services.planning_controller
    projects = controller(services)
    project = ok(projects.create("math"))
    homework = task(planning, "Homework", project.id, required_date=DAY)
    task(planning, "Laundry", None, required_date=DAY)
    day = DayScheduleController(planning, anchor_date=DAY, timezone="UTC")
    assert {row.name for row in ok(day.load()).unplaced} == {"Homework (mat)", "Laundry"}  # waiting to be scheduled
    assert day.make_schedule_for(DAY).ok
    snapshot = ok(day.load())
    assert {item.name for item in snapshot.timeline} == {"Homework (mat)", "Laundry"}
    assert {row.name for row in snapshot.rows} == {"Homework (mat)", "Laundry"}
    assert {item.display_name for item in snapshot.executables} == {"Homework (mat)", "Laundry"}
    detail = ok(DayOutcomeController(planning, services.execution_controller).detail(DAY))
    assert {card.name for card in detail.board.cards} == {"Homework (mat)", "Laundry"}
    month = ok(CalendarController(planning, mode="month", selected=DAY, timezone="UTC").load())
    assert {item.name for item in month.day(DAY).items} == {"Homework (mat)", "Laundry"}

    assert ok(planning.get_task(homework.id)).name == "Homework"  # display text only: the stored name is untouched
    current = ok(planning.get_project(project.id))
    ok(projects.update(project.id, "AP", "", expected_version=current.version))
    renamed = ok(day.load())
    assert {item.name for item in renamed.timeline} == {"Homework (AP)", "Laundry"}
    moved = ok(planning.get_task(homework.id))
    ok(planning.add_or_update_task(moved.model_copy(update={"project_id": None}), expected_version=moved.version))
    assert "Homework" in {row.name for row in ok(day.load()).rows}  # no project, no suffix


def test_an_unscheduled_project_task_can_still_be_edited_through_the_shared_form(services):
    """The row's display name never leaks into the stored name through the task form."""
    planning = services.planning_controller
    projects = controller(services)
    project = ok(projects.create("math"))
    ok(projects.add_task(project.id, draft("Homework"), DAY.isoformat()))
    day = DayScheduleController(planning, anchor_date=DAY, timezone="UTC")
    row = next(row for row in ok(day.load()).rows if row.name == "Homework (mat)")
    edit = ok(day.draft_for(row.ref))
    assert edit.name == "Homework" and edit.project_id == project.id
    ok(day.save_draft(replace(edit, priority="7"), editing=row.ref))
    assert [row.name for row in ok(day.load()).rows] == ["Homework (mat)"]  # still one suffix
    assert ok(planning.get_task(row.ref.id)).name == "Homework"
