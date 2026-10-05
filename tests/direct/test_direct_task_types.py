"""Task types and placement planning snapshots on the server schema (docs/productivity-redesign-plan.md,
contract A), through the direct services -- the same PlanningService as the desktop's SQLite path:

    - the live rows and the immutable record revisions round-trip the type and the whole snapshot;
    - a task can never name another account's type, and a failed unit of work leaves nothing behind;
    - revision 0013 gives existing tasks their deterministic type without inventing history, and
      downgrades and upgrades again on a disposable database.

SQLite by default; PostgreSQL with BACKEND_TESTS_ON_POSTGRES=1 (tests/direct/conftest.py)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.planning import workflow
from app.planning.errors import InvalidReferenceError
from app.planning.models import ScheduledTask, Task, derived_task_type_id
from app.planning.recurrence import occurrence_task_id
from backend import models, snapshots
from backend.migrate import current_revision, downgrade, head_revision, upgrade

MON, NEXT_MON = date(2026, 3, 2), date(2026, 3, 9)
SNAPSHOT = ("task_category", "task_name", "task_tags", "task_points", "task_estimate_minutes", "task_type_id",
            "task_type_label")


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def new_task(owner, name: str = "Reading", **fields) -> Task:
    return Task(**{"user_id": owner, "name": name, "category": "study", "tags": ["deep", "book"],
                   "estimated_duration_minutes": 45, "priority": 5, "points": 3, **fields})


def place(planning, task: Task, hour: int = 9) -> ScheduledTask:
    placement = ScheduledTask(task_id=task.id, user_id=task.user_id, planned_date=MON, timezone="UTC",
                              planned_start=at(MON, hour), planned_end=at(MON, hour) + timedelta(minutes=45))
    existing = planning.placements_for_date(MON)
    planning.replace_placements(MON, MON, [*existing, placement], expected_versions={p.id: p.version for p in existing})
    return planning.get_placement(placement.id)


def test_types_and_snapshots_round_trip_through_live_rows_and_revisions(alice, bob, engine) -> None:
    planning = alice.planning_service()
    task = planning.create_task(new_task(alice.user_id))
    assert task.task_type_id == derived_task_type_id(task.id)
    assert [(t.id, t.label, t.user_id) for t in planning.list_task_types()] == [
        (task.task_type_id, "Reading", alice.user_id)]

    placed = place(planning, task)
    expected = {"task_category": "study", "task_name": "Reading", "task_tags": ["deep", "book"], "task_points": 3,
                "task_estimate_minutes": 45, "task_type_id": task.task_type_id, "task_type_label": "Reading"}
    assert {name: getattr(placed, name) for name in SNAPSHOT} == expected

    # Later edits of the task and of its type's label never rewrite what was planned.
    edited = planning.update_task(task.model_copy(update={"name": "Skimming", "category": "leisure", "tags": [],
                                                          "points": 0}), expected_version=task.version)
    label = planning.list_task_types()[0]
    planning.update_task_type(label.model_copy(update={"label": "Books"}), expected_version=label.version)
    assert edited.task_type_id == task.task_type_id
    assert {name: getattr(planning.get_placement(placed.id), name) for name in SNAPSHOT} == expected

    # A move keeps the original snapshot on the tombstone; the replacement records the task as it is now.
    moved = workflow.reschedule_placement(
        planning, placed.id, expected_version=placed.version, planned_date=MON, timezone_name="UTC",
        planned_start=at(MON, 14), planned_end=at(MON, 14) + timedelta(minutes=45))
    assert {name: getattr(moved.previous, name) for name in SNAPSHOT} == expected
    assert (moved.replacement.task_name, moved.replacement.task_tags, moved.replacement.task_points,
            moved.replacement.task_type_label) == ("Skimming", [], 0, "Books")

    # The immutable revisions (the change feed, sync replay) hold the same records.
    with Session(engine) as session:
        entries = list(session.scalars(sa.select(models.ChangeLogEntry).where(
            models.ChangeLogEntry.user_id == alice.user_id).order_by(models.ChangeLogEntry.seq)))
        records = [(entry.entity_type, snapshots.decode(entry.revision)) for entry in entries]
    first_save = next(record for kind, record in records if kind == "placement" and record["id"] == str(placed.id))
    wire = {**expected, "task_type_id": str(task.task_type_id)}
    assert {name: first_save[name] for name in SNAPSHOT} == wire
    tombstone = [record for kind, record in records if kind == "placement" and record["id"] == str(placed.id)][-1]
    assert tombstone["deleted_at"] is not None and {name: tombstone[name] for name in SNAPSHOT} == wire
    assert [record["label"] for kind, record in records if kind == "task_type"] == ["Reading", "Books"]
    assert [record["task_type_id"] for kind, record in records if kind == "task"] == [str(task.task_type_id)] * 2

    # Another account never sees or references the type.
    theirs = bob.planning_service()
    assert theirs.list_task_types() == []
    with pytest.raises(InvalidReferenceError):
        theirs.create_task(new_task(bob.user_id, task_type_id=task.task_type_id))
    assert theirs.list_tasks() == []


def test_a_failed_unit_of_work_leaves_no_task_and_no_type(alice) -> None:
    planning = alice.planning_service()
    with pytest.raises(RuntimeError):
        with planning.transaction():
            planning.create_task(new_task(alice.user_id))
            raise RuntimeError("stop")
    assert planning.list_tasks() == [] and planning.list_task_types() == []


def test_0013_assigns_deterministic_types_without_inventing_history(blank_engine) -> None:
    upgrade(blank_engine, "0012")
    user, stamp = uuid.uuid4(), datetime(2026, 2, 1, 10, tzinfo=timezone.utc)
    reading_a, reading_b, series, segment = (uuid.uuid4() for _ in range(4))
    occurrence = occurrence_task_id(segment, NEXT_MON)
    placement = uuid.uuid4()
    instant = sa.DateTime(timezone=True)
    users = sa.table("users", sa.column("id", sa.Uuid()), sa.column("email"), sa.column("password_hash"),
                     sa.column("created_at", instant), sa.column("updated_at", instant), sa.column("version"))
    tasks = sa.table(
        "tasks", sa.column("user_id", sa.Uuid()), sa.column("id", sa.Uuid()), sa.column("name"), sa.column("category"),
        sa.column("estimated_duration_minutes"), sa.column("priority"), sa.column("required", sa.Boolean()),
        sa.column("required_date", sa.Date()), sa.column("recurrence_frequency"), sa.column("recurrence_interval"),
        sa.column("recurrence_start_date", sa.Date()), sa.column("recurrence_timezone"),
        sa.column("series_id", sa.Uuid()), sa.column("occurrence_slot", sa.Date()),
        sa.column("series_predecessor_id", sa.Uuid()), sa.column("created_at", instant),
        sa.column("updated_at", instant), sa.column("version"))
    placements = sa.table(
        "placements", sa.column("user_id", sa.Uuid()), sa.column("id", sa.Uuid()), sa.column("task_id", sa.Uuid()),
        sa.column("planned_date", sa.Date()), sa.column("timezone"), sa.column("planned_start", instant),
        sa.column("planned_end", instant), sa.column("score"), sa.column("optimization_metadata", sa.JSON()),
        sa.column("task_category"), sa.column("created_at", instant), sa.column("updated_at", instant),
        sa.column("version"))

    def task_row(task_id, name, **fields) -> dict:
        return {"user_id": user, "id": task_id, "name": name, "category": "study", "estimated_duration_minutes": 30,
                "priority": 5, "required": False, "required_date": None, "recurrence_frequency": None,
                "recurrence_interval": None, "recurrence_start_date": None, "recurrence_timezone": None,
                "series_id": None, "occurrence_slot": None, "series_predecessor_id": None, "created_at": stamp,
                "updated_at": stamp, "version": 3, **fields}

    weekly = {"recurrence_frequency": "weekly", "recurrence_interval": 1, "recurrence_timezone": "UTC"}
    with blank_engine.begin() as connection:
        connection.execute(sa.insert(users), {"id": user, "email": "alice@example.com", "password_hash": "x",
                                              "created_at": stamp, "updated_at": stamp, "version": 1})
        connection.execute(sa.insert(tasks), [
            task_row(reading_a, "Reading"), task_row(reading_b, "Reading"),
            task_row(series, "Standup", recurrence_start_date=MON, **weekly),
            task_row(segment, "Standup (later)", recurrence_start_date=NEXT_MON, series_predecessor_id=series, **weekly),
        ])
        connection.execute(sa.insert(tasks), [task_row(occurrence, "Standup (later)", required_date=NEXT_MON,
                                                       series_id=segment, occurrence_slot=NEXT_MON)])
        connection.execute(sa.insert(placements), {
            "user_id": user, "id": placement, "task_id": reading_a, "planned_date": MON, "timezone": "UTC",
            "planned_start": at(MON, 9), "planned_end": at(MON, 10), "score": 1.5, "optimization_metadata": {},
            "task_category": "study", "created_at": stamp, "updated_at": stamp, "version": 1})

    upgrade(blank_engine)
    with Session(blank_engine) as session:
        assert current_revision(session.connection()) == head_revision()
        stored = {row.id: row for row in session.scalars(sa.select(models.Task))}
        types = {row.id: row.label for row in session.scalars(sa.select(models.TaskType))}
        # Same-name tasks stay apart; a series, its continuation and their occurrences share the root's type.
        assert stored[reading_a].task_type_id == derived_task_type_id(reading_a) != stored[reading_b].task_type_id
        assert {stored[item].task_type_id for item in (series, segment, occurrence)} == {derived_task_type_id(series)}
        assert types == {derived_task_type_id(reading_a): "Reading", derived_task_type_id(reading_b): "Reading",
                         derived_task_type_id(series): "Standup"}
        assert {row.version for row in stored.values()} == {3}  # a derived identity, not a user mutation
        assert session.scalar(sa.select(sa.func.count()).select_from(models.RecordRevision)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(models.ChangeLogEntry)) == 0
        # The existing placement's snapshot stays unknown, apart from the category it already recorded.
        row = session.get(models.Placement, (user, placement))
        assert (row.task_category, row.task_name, row.task_tags_recorded, row.task_points, row.task_estimate_minutes,
                row.task_type_id, row.task_type_label) == ("study", None, False, None, None, None, None)

    downgrade(blank_engine, "0012")  # disposable databases only
    with blank_engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert "task_types" not in inspector.get_table_names()
        assert "task_type_id" not in {column["name"] for column in inspector.get_columns("tasks")}
        assert connection.execute(sa.text("SELECT COUNT(*) FROM tasks")).scalar_one() == 5
    upgrade(blank_engine)
    with Session(blank_engine) as session:
        assert session.get(models.Task, (user, occurrence)).task_type_id == derived_task_type_id(series)


def test_the_tracker_reads_completion_activity_from_the_server_schema(alice, bob, clock) -> None:
    planning, executions = alice.planning_service(), alice.execution_service()
    task = planning.create_task(new_task(alice.user_id))
    placed = place(planning, task)
    execution = executions.get_or_create_canonical_execution(task, placed)
    clock.now = at(MON, 9)
    executions.start(execution.id)
    clock.now = at(MON, 10)
    executions.complete(execution.id)
    planning.update_task(task.model_copy(update={"points": 9}), expected_version=task.version)
    clock.now = at(MON, 9) + timedelta(days=2)

    report = alice.productivity_service("UTC").build_tracker_report()
    assert (report.general.activity.completions, report.general.activity.known_points) == (1, 3)  # the snapshot
    assert report.general.highest_point_day.winners[0].start_date == MON
    assert report.general.counts.due_completion.value == 1.0 and report.completeness.complete
    assert [(view.type_id, view.label) for view in report.types] == [(task.task_type_id, "Reading")]
    assert report.general.current_green_streak.value == 0.0  # yesterday had nothing scheduled
    assert report.general.longest_green_streak.value == 1.0

    theirs = bob.productivity_service("UTC").build_tracker_report()  # another account sees none of it
    assert theirs.general.activity.completions == 0 and theirs.types == [] and theirs.time.days == []
