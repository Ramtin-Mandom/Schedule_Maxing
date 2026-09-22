"""Tests for app/planning/repository.py: exact round trips of every persisted
canonical entity (UUIDs, aware timestamps and their offsets, versions,
nullable fields, recurrence, deadlines, ownership, ordered collections),
relational storage (queryable columns and child tables rather than blobs),
close/reopen durability, atomic multi-row writes, and date-range queries.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.execution.db import get_connection
from app.planning.errors import InvalidEntityError
from app.planning.models import (
    FixedBlock,
    LocalTimeWindow,
    Project,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduledTask,
    Task,
)
from app.planning.repository import PlanningRepository

NY = ZoneInfo("America/New_York")


def full_task(**overrides) -> Task:
    """A task with every optional field populated."""
    dependency = overrides.pop("dependency_ids", [])
    defaults = dict(
        user_id=uuid.uuid4(),
        name="Write report",
        category="work",
        tags=["writing", "deep", "writing"],  # order and duplicates preserved
        estimated_duration_minutes=95,
        priority=9,
        required=True,
        required_date=date(2024, 6, 5),
        preferred_dates=[date(2024, 6, 5), date(2024, 6, 3)],
        preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=1440),
        dependency_ids=dependency,
        deadline=datetime(2024, 6, 5, 17, 30, 15, 123456, tzinfo=NY),  # non-UTC offset, microseconds
        recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, interval=2, weekdays=[4, 0], count=6),
        created_at=datetime(2024, 5, 1, 8, 0, 0, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 5, 2, 9, 30, tzinfo=timezone.utc),
        version=7,
    )
    defaults.update(overrides)
    return Task(**defaults)


def minimal_task(**overrides) -> Task:
    defaults = dict(name="Minimal", category="misc", estimated_duration_minutes=15, priority=1)
    defaults.update(overrides)
    return Task(**defaults)


def placement_for(task: Task, day: date, hour: int, *, minutes: int = 60, **overrides) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    defaults = dict(
        task_id=task.id, planned_date=day, timezone="UTC",
        planned_start=start, planned_end=start + timedelta(minutes=minutes), score=4.25,
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


def block_on(day: date, hour: int, *, label: str = "Class", minutes: int = 60, tz: str = "UTC") -> FixedBlock:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(tz))
    return FixedBlock(
        label=label, planned_date=day, timezone=tz, planned_start=start, planned_end=start + timedelta(minutes=minutes)
    )


# -----------------------------------------------------------------------------
# Round trips
# -----------------------------------------------------------------------------


def test_full_task_round_trip_is_exact(planning_repository: PlanningRepository) -> None:
    dependency = minimal_task(name="Dependency")
    task = full_task(dependency_ids=[dependency.id])
    with planning_repository.transaction():
        planning_repository.upsert_task(dependency)
        planning_repository.upsert_task(task)

    loaded = planning_repository.get_task(task.id)

    assert loaded == task
    assert loaded.model_dump(mode="json") == task.model_dump(mode="json")  # offsets/microseconds too
    assert loaded.deadline.utcoffset() == task.deadline.utcoffset()
    assert loaded.recurrence.weekdays == [0, 4]


def test_minimal_task_round_trip_keeps_nullable_fields_null(planning_repository: PlanningRepository) -> None:
    task = minimal_task()
    planning_repository.upsert_task(task)

    loaded = planning_repository.get_task(task.id)

    assert loaded == task
    assert loaded.user_id is None and loaded.project_id is None
    assert loaded.required_date is None and loaded.deadline is None
    assert loaded.preferred_time_window is None and loaded.recurrence is None
    assert loaded.tags == [] and loaded.preferred_dates == [] and loaded.dependency_ids == []


@pytest.mark.parametrize(
    "recurrence",
    [
        RecurrenceSpec(frequency=RecurrenceFrequency.DAILY),
        RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, interval=3, end_date=date(2024, 12, 31)),
        RecurrenceSpec(frequency=RecurrenceFrequency.MONTHLY, day_of_month=31, count=12),
        RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, weekdays=[6]),
    ],
)
def test_recurrence_variants_round_trip(planning_repository: PlanningRepository, recurrence: RecurrenceSpec) -> None:
    task = minimal_task(recurrence=recurrence)
    planning_repository.upsert_task(task)
    assert planning_repository.get_task(task.id).recurrence == recurrence


def test_task_fields_are_stored_relationally_not_as_blobs(planning_repository: PlanningRepository, connection) -> None:
    dependency = minimal_task(name="Dependency")
    task = full_task(dependency_ids=[dependency.id])
    planning_repository.upsert_task(dependency)
    planning_repository.upsert_task(task)
    task_id = str(task.id)

    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["required_date"] == "2024-06-05"
    assert row["preferred_window_start_minute"] == 540 and row["preferred_window_end_minute"] == 1440
    assert row["deadline_utc"] == "2024-06-05T21:30:15.123456Z"
    assert row["recurrence_frequency"] == "weekly" and row["recurrence_count"] == 6
    assert [r["tag"] for r in connection.execute(
        "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY position", (task_id,)
    )] == ["writing", "deep", "writing"]
    assert [r[0] for r in connection.execute(
        "SELECT preferred_date FROM task_preferred_dates WHERE task_id = ? ORDER BY position", (task_id,)
    )] == ["2024-06-05", "2024-06-03"]
    assert [r[0] for r in connection.execute(
        "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?", (task_id,)
    )] == [str(dependency.id)]
    assert [r[0] for r in connection.execute(
        "SELECT weekday FROM task_recurrence_weekdays WHERE task_id = ? ORDER BY weekday", (task_id,)
    )] == [0, 4]


def test_overwriting_a_task_replaces_its_child_rows(planning_repository: PlanningRepository, connection) -> None:
    task = full_task(required_date=None)
    planning_repository.upsert_task(task)

    edited = task.model_copy(update={"tags": ["only"], "preferred_dates": [], "recurrence": None, "deadline": None})
    planning_repository.upsert_task(edited)

    assert planning_repository.get_task(task.id) == edited
    assert connection.execute("SELECT COUNT(*) FROM task_recurrence_weekdays").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM task_preferred_dates").fetchone()[0] == 0


def test_project_round_trip(planning_repository: PlanningRepository) -> None:
    project = Project(
        user_id=uuid.uuid4(), name="Capstone", description=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), updated_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        version=4,
    )
    planning_repository.upsert_project(project)
    task = minimal_task(project_id=project.id)
    planning_repository.upsert_task(task)

    assert planning_repository.get_project(project.id) == project
    assert planning_repository.get_task(task.id).project_id == project.id
    assert planning_repository.task_ids_for_project(project.id) == [task.id]


def test_fixed_block_round_trip_keeps_local_offset(planning_repository: PlanningRepository) -> None:
    block = block_on(date(2024, 6, 3), 8, label="Lecture", tz="America/New_York")
    planning_repository.upsert_fixed_block(block)

    [loaded] = planning_repository.list_fixed_blocks(date(2024, 6, 3), date(2024, 6, 3))

    assert loaded == block
    assert loaded.planned_start.isoformat() == block.planned_start.isoformat()


def test_placement_round_trip_with_metadata(planning_repository: PlanningRepository) -> None:
    task = minimal_task()
    planning_repository.upsert_task(task)
    placement = placement_for(
        task, date(2024, 6, 3), 10,
        optimization_metadata={"mode": "precise_greedy", "candidates": [1, 2.5, None], "nested": {"ok": True}},
        version=2,
    )
    planning_repository.upsert_placement(placement)

    assert planning_repository.get_placements([placement.id])[placement.id] == placement


def test_placement_metadata_must_be_json(planning_repository: PlanningRepository) -> None:
    task = minimal_task()
    planning_repository.upsert_task(task)

    with pytest.raises(InvalidEntityError, match="JSON"):
        planning_repository.upsert_placement(placement_for(task, date(2024, 6, 3), 9, optimization_metadata={"x": object()}))


def test_returned_models_are_independent_snapshots(planning_repository: PlanningRepository) -> None:
    task = full_task(required_date=None)
    planning_repository.upsert_task(task)

    loaded = planning_repository.get_task(task.id)
    loaded.name = "mutated"
    loaded.tags.append("mutated")
    task.preferred_dates.clear()  # mutating the saved input does nothing either

    again = planning_repository.get_task(task.id)
    assert again.name == "Write report"
    assert again.tags == ["writing", "deep", "writing"]
    assert again.preferred_dates == [date(2024, 6, 5), date(2024, 6, 3)]


# -----------------------------------------------------------------------------
# Durability and atomicity
# -----------------------------------------------------------------------------


def test_independent_close_and_reopen_restores_everything(db_path: Path) -> None:
    connection = get_connection(db_path)
    repository = PlanningRepository(connection)
    project = Project(name="P")
    dependency = minimal_task(name="Dep", project_id=project.id)
    task = full_task(dependency_ids=[dependency.id], project_id=project.id)
    block = block_on(date(2024, 6, 3), 7)
    placement = placement_for(task, date(2024, 6, 5), 9, optimization_metadata={"k": "v"})
    with repository.transaction():
        repository.upsert_project(project)
        repository.upsert_task(dependency)
        repository.upsert_task(task)
        repository.upsert_fixed_block(block)
        repository.upsert_placement(placement)
    connection.close()

    reopened = get_connection(db_path)
    try:
        repository = PlanningRepository(reopened)
        assert repository.list_projects() == [project]
        assert {t.id: t for t in repository.list_tasks()} == {dependency.id: dependency, task.id: task}
        assert repository.list_fixed_blocks(date(2024, 6, 1), date(2024, 6, 30)) == [block]
        assert repository.list_placements(date(2024, 6, 1), date(2024, 6, 30)) == [placement]
    finally:
        reopened.close()


def test_multi_row_write_rolls_back_entirely_after_an_injected_failure(
    planning_repository: PlanningRepository, connection
) -> None:
    first, second = minimal_task(name="First"), full_task(required_date=None)

    with pytest.raises(RuntimeError):
        with planning_repository.transaction():
            planning_repository.upsert_task(first)
            planning_repository.upsert_task(second)  # task row + four child tables
            raise RuntimeError("injected")

    for table in ("tasks", "task_tags", "task_preferred_dates", "task_recurrence_weekdays"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_deferred_foreign_keys_allow_any_order_but_fail_at_commit_when_missing(
    planning_repository: PlanningRepository, connection
) -> None:
    dependency = minimal_task(name="Dep")
    dependent = minimal_task(name="Dependent", dependency_ids=[dependency.id])
    with planning_repository.transaction():
        planning_repository.upsert_task(dependent)  # before its dependency: fine inside one transaction
        planning_repository.upsert_task(dependency)
    assert planning_repository.get_task(dependent.id).dependency_ids == [dependency.id]

    orphan = minimal_task(name="Orphan", dependency_ids=[uuid.uuid4()])
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        planning_repository.upsert_task(orphan)
    assert planning_repository.get_task(orphan.id) is None


def test_deleting_a_task_cascades_to_its_children_and_placements_only(
    planning_repository: PlanningRepository, connection
) -> None:
    keep, remove = minimal_task(name="Keep", tags=["a"]), full_task(required_date=None)
    planning_repository.upsert_task(keep)
    planning_repository.upsert_task(remove)
    planning_repository.upsert_placement(placement_for(keep, date(2024, 6, 3), 9))
    planning_repository.upsert_placement(placement_for(remove, date(2024, 6, 3), 11))

    assert planning_repository.delete_tasks([remove.id]) == 1

    assert [task.id for task in planning_repository.list_tasks()] == [keep.id]
    assert [p.task_id for p in planning_repository.list_placements(date(2024, 6, 3), date(2024, 6, 3))] == [keep.id]
    assert connection.execute("SELECT COUNT(*) FROM task_tags").fetchone()[0] == 1


# -----------------------------------------------------------------------------
# Date/range queries
# -----------------------------------------------------------------------------


def test_range_queries_are_inclusive_ordered_and_isolated(planning_repository: PlanningRepository) -> None:
    task = minimal_task()
    planning_repository.upsert_task(task)
    days = [date(2024, 6, 2), date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    for day in reversed(days):
        planning_repository.upsert_placement(placement_for(task, day, 15))
        planning_repository.upsert_placement(placement_for(task, day, 9))
        planning_repository.upsert_fixed_block(block_on(day, 12))

    placements = planning_repository.list_placements(date(2024, 6, 3), date(2024, 6, 4))
    blocks = planning_repository.list_fixed_blocks(date(2024, 6, 3), date(2024, 6, 4))

    assert [(p.planned_date, p.planned_start.hour) for p in placements] == [
        (date(2024, 6, 3), 9), (date(2024, 6, 3), 15), (date(2024, 6, 4), 9), (date(2024, 6, 4), 15),
    ]
    assert [b.planned_date for b in blocks] == [date(2024, 6, 3), date(2024, 6, 4)]


def test_ordering_uses_utc_instants_across_offsets(planning_repository: PlanningRepository) -> None:
    day = date(2024, 6, 3)
    later_local = FixedBlock(
        label="NY 08:00 (12:00Z)", planned_date=day, timezone="America/New_York",
        planned_start=datetime(2024, 6, 3, 8, tzinfo=NY), planned_end=datetime(2024, 6, 3, 9, tzinfo=NY),
    )
    earlier_utc = FixedBlock(
        label="10:00Z", planned_date=day, timezone="UTC",
        planned_start=datetime(2024, 6, 3, 10, tzinfo=timezone.utc), planned_end=datetime(2024, 6, 3, 11, tzinfo=timezone.utc),
    )
    planning_repository.upsert_fixed_block(later_local)
    planning_repository.upsert_fixed_block(earlier_utc)

    assert [b.label for b in planning_repository.list_fixed_blocks(day, day)] == ["10:00Z", "NY 08:00 (12:00Z)"]


def test_task_eligibility_query(planning_repository: PlanningRepository) -> None:
    start, end = date(2024, 6, 3), date(2024, 6, 9)
    floating = minimal_task(name="floating")
    pinned_inside = minimal_task(name="pinned inside", required_date=date(2024, 6, 9))
    pinned_outside = minimal_task(name="pinned outside", required_date=date(2024, 6, 10))
    deadline_on_start = minimal_task(name="deadline on start", deadline=datetime(2024, 6, 3, 0, 0, tzinfo=timezone.utc))
    deadline_before = minimal_task(name="deadline before", deadline=datetime(2024, 6, 2, 23, 59, tzinfo=timezone.utc))
    # 2024-06-02 20:30 in New York is 2024-06-03 00:30 UTC: allocation compares the UTC date.
    deadline_offset = minimal_task(name="deadline offset", deadline=datetime(2024, 6, 2, 20, 30, tzinfo=NY))
    # required_date governs even when a deadline would also allow the range.
    pinned_outside_with_deadline = minimal_task(
        name="pinned outside + deadline", required_date=date(2024, 5, 1), deadline=datetime(2024, 7, 1, tzinfo=timezone.utc)
    )
    for task in (floating, pinned_inside, pinned_outside, deadline_on_start, deadline_before, deadline_offset,
                 pinned_outside_with_deadline):
        planning_repository.upsert_task(task)

    names = {task.name for task in planning_repository.list_tasks_eligible_for_range(start, end)}

    assert names == {"floating", "pinned inside", "deadline on start", "deadline offset"}
