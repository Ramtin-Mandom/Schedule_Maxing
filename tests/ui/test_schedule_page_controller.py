"""Headless tests for app/ui/schedule_page_controller.py -- the presenter every
desktop SchedulePage callback delegates to (UI -> presenter -> PlanningController
-> PlanningService -> repository -> SQLite). They verify that mutations are
committed to SQLite before the page is redrawn, that selection and
dependencies are UUID-based (duplicate names, reordered rows), that failed
writes/generation keep both the database and the visible state consistent,
and how saved schedules are labelled after a restart.

Every database is a temporary file; nothing touches the real data directory.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import FixedBlock, Task
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController
from app.planning.csv_import import ImportMode
from app.ui.schedule_page_controller import ResetScope, RowRef, SchedulePageController

MON = date(2024, 6, 3)


class Stack:
    """One 'app session' on a database file: the same objects the desktop app wires up."""

    def __init__(self, db_path: Path, project_root: Path, *, days: int = 7, anchor: date = MON, tz: str = "UTC") -> None:
        self.connection = get_connection(db_path)
        self.service = PlanningService(PlanningRepository(self.connection))
        self.planning = PlanningController(service=self.service, timezone=tz, project_root=str(project_root))
        self.page = SchedulePageController(self.planning, number_of_days=days, anchor_date=anchor, timezone=tz)
        self.executions = ExecutionController(ExecutionService(ExecutionRepository(self.connection)))

    def close(self) -> None:
        self.connection.close()


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "desktop.db"


@pytest.fixture
def stack(db_path, tmp_path):
    session = Stack(db_path, tmp_path)
    try:
        yield session
    finally:
        session.close()


def flexible(name="Study", *, day="1", start="480", end="720", duration="60", priority="5", tag="math", category="study"):
    return {
        "name": name, "day": day, "category": category, "tag": tag, "fixed": "False",
        "start_time": start, "end_time": end, "duration": duration, "priority": priority,
    }


def fixed(name="Lecture", *, day="1", start="600", end="660"):
    return {
        "name": name, "day": day, "category": "event", "tag": "uni", "fixed": "True",
        "start_time": start, "end_time": end, "duration": "", "priority": "",
    }


def ok(result):
    assert result.ok, result.error
    return result.value


def task_rows(snapshot, name=None):
    return [row for row in snapshot.rows if row.ref.kind == "task" and (name is None or row.name == name)]


# -----------------------------------------------------------------------------
# Create / edit / delete go through the service and are re-read from SQLite
# -----------------------------------------------------------------------------


def test_adding_a_flexible_task_commits_a_canonical_task(stack: Stack) -> None:
    snapshot = ok(stack.page.submit_task_form(flexible(day="3")))

    [stored] = stack.service.list_tasks()
    assert stored.name == "Study" and stored.tags == ["math"]
    assert stored.preferred_dates == [date(2024, 6, 5)]  # day 3 of a page anchored on Monday June 3
    assert (stored.preferred_time_window.start_minute, stored.preferred_time_window.end_minute) == (480, 720)
    assert stored.estimated_duration_minutes == 60
    [row] = task_rows(snapshot)
    assert row.ref == RowRef("task", stored.id)
    assert row.date == date(2024, 6, 5)
    assert snapshot.flexible_count == 1


def test_adding_a_fixed_block_uses_the_page_date_and_timezone(db_path, tmp_path) -> None:
    session = Stack(db_path, tmp_path, tz="America/New_York")
    try:
        ok(session.page.submit_task_form(fixed(day="2", start="480", end="540")))
        [block] = session.service.fixed_blocks_for_date(date(2024, 6, 4))
        # 08:00-09:00 New York (EDT, UTC-4) on June 4.
        assert block.planned_start == datetime(2024, 6, 4, 12, tzinfo=timezone.utc)
        assert block.planned_end == datetime(2024, 6, 4, 13, tzinfo=timezone.utc)
        assert block.timezone == "America/New_York"
        [row] = ok(session.page.load()).rows
        assert row.time_text == "08:00 - 09:00"
    finally:
        session.close()


@pytest.mark.parametrize(
    "values, message",
    [
        (flexible(name=""), "Name is required"),
        (flexible(day="8"), "Day must be between 1 and 7"),
        (flexible(start="490"), "multiples of 30"),
        (flexible(duration="45"), "multiple of 30"),
        (flexible(priority="11"), "Priority"),
        (flexible(tag=""), "Tag is required"),
        (flexible(category="nope"), "Category"),
        (flexible(start="720", end="480"), "Start time must be smaller"),
    ],
)
def test_invalid_form_values_write_nothing(stack: Stack, values, message) -> None:
    ok(stack.page.submit_task_form(flexible(name="Existing")))

    result = stack.page.submit_task_form(values)

    assert not result.ok and message in result.error
    assert [task.name for task in stack.service.list_tasks()] == ["Existing"]
    assert [row.name for row in result.value.rows] == ["Existing"]  # committed state, re-read


def test_overlapping_fixed_blocks_are_rejected(stack: Stack) -> None:
    ok(stack.page.submit_task_form(fixed("Lecture", start="600", end="720")))
    result = stack.page.submit_task_form(fixed("Lab", start="690", end="750"))
    assert not result.ok and "overlaps" in result.error
    assert [b.label for b in stack.service.fixed_blocks_for_date(MON)] == ["Lecture"]
    ok(stack.page.submit_task_form(fixed("Lab", start="720", end="780")))  # touching is fine


def test_editing_a_task_keeps_its_id_and_bumps_its_version(stack: Stack) -> None:
    snapshot = ok(stack.page.submit_task_form(flexible("Draft", day="1")))
    ref = task_rows(snapshot)[0].ref
    original = stack.service.get_task(ref.id)

    state = ok(stack.page.form_state_for(ref))
    edited_values = dict(state.values, name="Final", day="4", priority="9")
    snapshot = ok(stack.page.submit_task_form(edited_values, dependency_ids=state.dependency_ids, editing=ref))

    stored = stack.service.get_task(ref.id)
    assert (stored.id, stored.name, stored.priority) == (original.id, "Final", 9)
    assert stored.preferred_dates == [date(2024, 6, 6)]
    assert stored.version == original.version + 1 and stored.created_at == original.created_at
    assert [row.ref for row in task_rows(snapshot)] == [ref]


def test_editing_a_fixed_block_can_move_it_and_kind_cannot_change(stack: Stack) -> None:
    snapshot = ok(stack.page.submit_task_form(fixed("Gym", day="1", start="360", end="420")))
    ref = snapshot.rows[0].ref
    state = ok(stack.page.form_state_for(ref))
    assert state.values["start_time"] == "360" and state.values["fixed"] == "True"

    ok(stack.page.submit_task_form(dict(state.values, day="2"), editing=ref))
    assert stack.service.fixed_blocks_for_date(MON) == []
    assert [b.id for b in stack.service.fixed_blocks_for_date(date(2024, 6, 4))] == [ref.id]

    result = stack.page.submit_task_form(flexible(), editing=ref)
    assert not result.ok and "cannot be turned into" in result.error


def test_duplicate_names_and_dependencies_are_resolved_by_id(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Study", day="1")))
    snapshot = ok(stack.page.submit_task_form(flexible("Study", day="2")))
    first, second = sorted(task_rows(snapshot, "Study"), key=lambda row: row.date)
    assert first.ref != second.ref

    ok(stack.page.submit_task_form(flexible("Exam prep", day="3"), dependency_ids=[second.ref.id]))

    [dependent] = [t for t in stack.service.list_tasks() if t.name == "Exam prep"]
    assert dependent.dependency_ids == [second.ref.id]
    assert ok(stack.page.describe_tasks([second.ref.id])) == ["Study (Tue Jun 4)"]


def test_delete_targets_the_selected_id_even_after_rows_are_reordered(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Study", day="3")))
    snapshot = ok(stack.page.submit_task_form(flexible("Study", day="1")))
    # Rows are ordered by date, not insertion; pick the Wednesday one by id.
    wednesday = next(row for row in snapshot.rows if row.date == date(2024, 6, 5))

    snapshot = ok(stack.page.delete(wednesday.ref))

    [remaining] = stack.service.list_tasks()
    assert remaining.preferred_dates == [MON]
    assert [row.ref.id for row in snapshot.rows] == [remaining.id]


def test_deleting_a_task_others_depend_on_fails_with_names_and_changes_nothing(stack: Stack) -> None:
    base = task_rows(ok(stack.page.submit_task_form(flexible("Read", day="1"))))[0]
    ok(stack.page.submit_task_form(flexible("Summarise", day="2"), dependency_ids=[base.ref.id]))

    result = stack.page.delete(base.ref)

    assert not result.ok
    assert "Summarise (Tue Jun 4)" in result.error
    assert len(stack.service.list_tasks()) == 2
    assert len(result.value.rows) == 2


def test_self_dependency_is_rejected_by_the_model(stack: Stack) -> None:
    ref = task_rows(ok(stack.page.submit_task_form(flexible("Loop"))))[0].ref
    state = ok(stack.page.form_state_for(ref))
    result = stack.page.submit_task_form(state.values, dependency_ids=[ref.id], editing=ref)
    assert not result.ok and "depend on itself" in result.error


# -----------------------------------------------------------------------------
# Make Schedule: canonical engine, atomic save, re-read
# -----------------------------------------------------------------------------


def test_make_schedule_saves_placements_and_shows_them_as_current(stack: Stack) -> None:
    ok(stack.page.submit_task_form(fixed("Lecture", day="1", start="480", end="600")))
    ok(stack.page.submit_task_form(flexible("Study", day="1", start="480", end="720")))

    run = ok(stack.page.make_schedule())

    [placement] = stack.service.placements_for_date(MON)
    assert run.placed_count == 1 and run.unscheduled == []
    assert placement.planned_start >= datetime(2024, 6, 3, 10, tzinfo=timezone.utc)  # after the lecture
    assert run.snapshot.day_status == {MON: DayResultStatus.GENERATED}
    assert "current" in run.snapshot.status_text
    assert [item.mode for item in run.snapshot.canvas_items if item.name == "Study"] == ["optimized"]
    [executable] = run.snapshot.executables
    assert executable.placement == placement and executable.task.name == "Study"


def test_rescheduling_reuses_placement_ids_timestamps_and_scores(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Study", day="2")))
    ok(stack.page.make_schedule())
    before = stack.service.placements_for_date(date(2024, 6, 4))

    ok(stack.page.make_schedule())

    assert stack.service.placements_for_date(date(2024, 6, 4)) == before  # same ids, instants, scores, versions


def test_make_schedule_never_touches_dates_outside_the_page(db_path, tmp_path) -> None:
    week = Stack(db_path, tmp_path)
    try:
        other_day = SchedulePageController(week.planning, number_of_days=1, anchor_date=date(2024, 6, 20), timezone="UTC")
        ok(other_day.submit_task_form(flexible("Later")))
        ok(other_day.make_schedule())
        later = week.service.placements_for_date(date(2024, 6, 20))
        assert len(later) == 1

        ok(week.page.submit_task_form(flexible("This week", day="1")))
        ok(week.page.make_schedule())

        assert week.service.placements_for_date(date(2024, 6, 20)) == later
        assert "Later" not in [row.name for row in ok(week.page.load()).rows]  # planned for another range
    finally:
        week.close()


def test_failed_save_keeps_the_previous_schedule_and_its_status(stack: Stack, monkeypatch) -> None:
    ok(stack.page.submit_task_form(flexible("Study", day="1")))
    ok(stack.page.make_schedule())
    before = stack.service.placements_for_date(MON)

    def fail(*args, **kwargs):
        raise RuntimeError("disk I/O error (injected)")

    monkeypatch.setattr(stack.service, "replace_placements", fail)
    result = stack.page.make_schedule()

    assert not result.ok
    assert "injected" in result.error and "Nothing was saved" in result.error
    monkeypatch.undo()
    assert stack.service.placements_for_date(MON) == before
    assert ok(stack.planning.day_state(MON)).status == DayResultStatus.GENERATED


def test_failed_generation_saves_nothing(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Keep", day="2")))
    ok(stack.page.make_schedule())
    before = stack.service.placements_for_date(date(2024, 6, 4))
    # A required task that fits the day's aggregate capacity but no single free interval.
    ok(stack.planning.add_or_update_task(Task(
        name="Fragmented", category="study", estimated_duration_minutes=1000, priority=5,
        required=True, required_date=MON,
    )))
    ok(stack.planning.save_fixed_block(FixedBlock(
        label="Blocker", planned_date=MON, timezone="UTC",
        planned_start=datetime(2024, 6, 3, 11, 40, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, 13, 20, tzinfo=timezone.utc),
    )))

    result = stack.page.make_schedule()

    assert not result.ok and "Could not generate 2024-06-03" in result.error
    assert stack.service.placements_for_date(date(2024, 6, 4)) == before
    assert stack.service.placements_for_date(MON) == []


def test_editing_after_scheduling_marks_the_saved_schedule_stale(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Study", day="1")))
    ok(stack.page.make_schedule())

    snapshot = ok(stack.page.submit_task_form(flexible("New", day="2")))

    assert snapshot.day_status == {MON: DayResultStatus.STALE}
    assert "out of date" in snapshot.status_text
    assert [item.mode for item in snapshot.canvas_items if item.name == "Study"] == ["stale"]
    assert len(stack.service.placements_for_date(MON)) == 1  # kept, only relabelled


# -----------------------------------------------------------------------------
# Restart
# -----------------------------------------------------------------------------


def test_reopen_restores_everything_and_labels_the_old_schedule_stale(db_path, tmp_path) -> None:
    first = Stack(db_path, tmp_path)
    ok(first.page.submit_task_form(fixed("Lecture", day="1", start="480", end="540")))
    ok(first.page.submit_task_form(flexible("Study", day="1")))
    ok(first.page.submit_task_form(flexible("Study", day="2")))
    run = ok(first.page.make_schedule())
    saved_rows, saved_executables = run.snapshot.rows, run.snapshot.executables
    first.close()

    second = Stack(db_path, tmp_path)
    try:
        snapshot = ok(second.page.load())
        assert snapshot.rows == saved_rows
        assert [e.placement for e in snapshot.executables] == [e.placement for e in saved_executables]
        # Transient allocation state is gone: never shown as current.
        assert set(snapshot.day_status.values()) == {DayResultStatus.STALE}
        assert all(item.mode != "optimized" for item in snapshot.canvas_items)

        rerun = ok(second.page.make_schedule())
        assert set(rerun.snapshot.day_status.values()) == {DayResultStatus.GENERATED}
        assert [e.placement.id for e in rerun.snapshot.executables] == [e.placement.id for e in saved_executables]
    finally:
        second.close()


# -----------------------------------------------------------------------------
# Reset scopes
# -----------------------------------------------------------------------------


def _schedule_with_history(stack: Stack):
    ok(stack.page.submit_task_form(fixed("Lecture", day="1", start="480", end="540")))
    ok(stack.page.submit_task_form(flexible("Study", day="1")))
    snapshot = ok(stack.page.make_schedule()).snapshot
    [executable] = snapshot.executables
    execution = ok(stack.executions.get_or_create_canonical_execution(executable.task, executable.placement))
    ok(stack.executions.start(execution.id))
    return execution


def test_reset_schedule_only_keeps_tasks_blocks_and_history(stack: Stack) -> None:
    execution = _schedule_with_history(stack)

    snapshot = ok(stack.page.reset(ResetScope.SCHEDULE))

    assert stack.service.placements_for_date(MON) == []
    assert len(stack.service.list_tasks()) == 1 and len(stack.service.fixed_blocks_for_date(MON)) == 1
    assert snapshot.executables == [] and snapshot.day_status == {}
    assert ok(stack.executions.get_execution(execution.id)).status.value == "in_progress"


def test_reset_planning_data_is_scoped_to_the_page_and_keeps_history(db_path, tmp_path) -> None:
    stack = Stack(db_path, tmp_path)
    try:
        execution = _schedule_with_history(stack)
        next_week = SchedulePageController(stack.planning, number_of_days=7, anchor_date=date(2024, 6, 10), timezone="UTC")
        ok(next_week.submit_task_form(flexible("Next week")))
        ok(next_week.submit_task_form(fixed("Next lecture")))

        snapshot = ok(stack.page.reset(ResetScope.PLANNING_DATA))

        assert snapshot.rows == []
        assert [t.name for t in stack.service.list_tasks()] == ["Next week"]
        assert len(stack.service.fixed_blocks_for_date(date(2024, 6, 10))) == 1
        history = ok(stack.executions.get_execution(execution.id))
        assert history.task_name == "Study" and history.status.value == "in_progress"
        assert len(ExecutionService(ExecutionRepository(stack.connection)).list_sessions(execution.id)) == 1
    finally:
        stack.close()


def test_reset_is_refused_atomically_when_a_task_outside_depends_on_one_inside(db_path, tmp_path) -> None:
    stack = Stack(db_path, tmp_path)
    try:
        inside = task_rows(ok(stack.page.submit_task_form(flexible("Inside"))))[0].ref
        ok(stack.page.make_schedule())
        next_week = SchedulePageController(stack.planning, number_of_days=7, anchor_date=date(2024, 6, 10), timezone="UTC")
        ok(next_week.submit_task_form(flexible("Outside"), dependency_ids=[inside.id]))

        result = stack.page.reset(ResetScope.PLANNING_DATA)

        assert not result.ok and "depend" in result.error
        assert len(stack.service.list_tasks()) == 2
        assert len(stack.service.placements_for_date(MON)) == 1  # the placement deletion rolled back too
    finally:
        stack.close()


def test_reset_description_is_explicit_about_scope_and_history(stack: Stack) -> None:
    text = stack.page.reset_description(ResetScope.SCHEDULE)
    assert "2024-06-03 to 2024-06-09" in text and "NOT deleted" in text
    assert "fixed blocks, and tasks" in stack.page.reset_description(ResetScope.PLANNING_DATA)


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------


def test_anchor_date_is_explicit_and_validated(stack: Stack) -> None:
    ok(stack.page.submit_task_form(flexible("Monday task", day="1")))

    assert not stack.page.set_anchor_date("06/10/2024").ok
    snapshot = ok(stack.page.set_anchor_date("2024-06-10"))

    assert (snapshot.start_date, snapshot.end_date) == (date(2024, 6, 10), date(2024, 6, 16))
    assert snapshot.rows == []
    assert [row.name for row in ok(stack.page.set_anchor_date(MON)).rows] == ["Monday task"]


def test_unknown_task_or_block_refs_fail_cleanly(stack: Stack) -> None:
    assert not stack.page.form_state_for(RowRef("task", uuid.uuid4())).ok
    assert not stack.page.form_state_for(RowRef("block", uuid.uuid4())).ok


# -----------------------------------------------------------------------------
# CSV import / export through the page (anchor = the page's start date)
# -----------------------------------------------------------------------------

CSV_HEADER = "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"


def write_csv(path: Path, body: str) -> str:
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return str(path)


def test_import_uses_the_page_anchor_and_marks_saved_schedules_stale(stack: Stack, tmp_path: Path) -> None:
    ok(stack.page.submit_task_form(flexible("Existing", day="1")))
    ok(stack.page.make_schedule())

    run = ok(stack.page.import_csv(write_csv(tmp_path / "a.csv", "2,Imported,study,t,false,540,720,60,5,\n"), ImportMode.APPEND))

    imported = next(t for t in stack.service.list_tasks() if t.name == "Imported")
    assert imported.preferred_dates == [date(2024, 6, 4)]  # day 2 of the page anchored on June 3
    assert "Imported 1 task(s)" in run.summary
    assert run.snapshot.day_status == {MON: DayResultStatus.STALE}


def test_invalid_import_changes_nothing_and_returns_the_committed_view(stack: Stack, tmp_path: Path) -> None:
    ok(stack.page.submit_task_form(flexible("Existing", day="1")))
    path = write_csv(tmp_path / "bad.csv", "1,Good,study,t,false,540,720,60,5,\n9,Bad,study,t,false,540,720,60,0,\n")

    result = stack.page.import_csv(path, ImportMode.REPLACE)

    assert not result.ok and "line 3" in result.error
    assert [task.name for task in stack.service.list_tasks()] == ["Existing"]
    assert [row.name for row in result.value.rows] == ["Existing"]


def test_import_description_states_anchor_mode_and_history(stack: Stack) -> None:
    assert "2024-06-03" in stack.page.import_description(ImportMode.APPEND)
    assert "Execution history is kept" in stack.page.import_description(ImportMode.REPLACE)
