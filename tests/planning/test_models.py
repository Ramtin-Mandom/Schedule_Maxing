"""Tests for app/planning/models.py: canonical planning models."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.planning.models import (
    DaySchedule,
    DayScheduleOutput,
    FixedBlock,
    LocalTimeWindow,
    PlanningDocument,
    Project,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduledTask,
    Task,
    TaskRegistry,
    UnscheduledEntry,
    UnscheduledReasonCode,
    compute_total_score,
    project_scheduled_task_display,
)


UTC_NOW = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _task(**overrides) -> Task:
    defaults = dict(name="Study", category="study", estimated_duration_minutes=60, priority=5)
    defaults.update(overrides)
    return Task(**defaults)


def _placement(task_id: uuid.UUID, **overrides) -> ScheduledTask:
    defaults = dict(
        task_id=task_id,
        planned_date=date(2024, 6, 1),
        timezone="UTC",
        planned_start=datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc),
        score=5.0,
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


# -----------------------------------------------------------------------------
# Task identity and required fields
# -----------------------------------------------------------------------------


def test_task_gets_a_uuid_by_default():
    task = _task()
    assert isinstance(task.id, uuid.UUID)


def test_task_ids_are_unique_per_instance():
    assert _task().id != _task().id


def test_task_id_survives_serialization_round_trip():
    task = _task()
    restored = Task.model_validate_json(task.model_dump_json())
    assert restored.id == task.id


def test_duplicate_task_names_get_distinct_ids():
    a = _task(name="Study Session")
    b = _task(name="Study Session")
    assert a.id != b.id
    assert a.name == b.name


def test_task_duration_must_be_positive():
    with pytest.raises(ValidationError):
        _task(estimated_duration_minutes=0)


def test_task_priority_must_be_in_range():
    with pytest.raises(ValidationError):
        _task(priority=11)
    with pytest.raises(ValidationError):
        _task(priority=0)


def test_task_optional_project_and_user_ids():
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task = _task(project_id=project_id, user_id=user_id)
    assert task.project_id == project_id
    assert task.user_id == user_id


def test_task_project_and_user_ids_default_to_none():
    task = _task()
    assert task.project_id is None
    assert task.user_id is None


def test_task_cannot_depend_on_itself():
    task_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        Task(
            id=task_id,
            name="Study",
            category="study",
            estimated_duration_minutes=30,
            priority=1,
            dependency_ids=[task_id],
        )


def test_task_required_flag_and_required_date():
    task = _task(required=True, required_date=date(2024, 6, 5))
    assert task.required is True
    assert task.required_date == date(2024, 6, 5)


def test_task_deadline_must_be_aware():
    with pytest.raises(ValidationError):
        _task(deadline=datetime(2024, 6, 1, 12, 0))  # naive


def test_task_deadline_accepts_aware_datetime():
    task = _task(deadline=UTC_NOW)
    assert task.deadline == UTC_NOW


def test_task_created_updated_normalized_to_utc():
    eastern = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    task = _task(created_at=eastern, updated_at=eastern)
    assert task.created_at.tzinfo == timezone.utc
    assert task.created_at == eastern


def test_task_version_defaults_to_one_and_must_be_positive():
    task = _task()
    assert task.version == 1
    with pytest.raises(ValidationError):
        _task(version=0)


# -----------------------------------------------------------------------------
# RecurrenceSpec (model only, no expansion)
# -----------------------------------------------------------------------------


def test_recurrence_weekly_with_weekdays():
    spec = RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, weekdays=[0, 2, 4])
    assert spec.weekdays == [0, 2, 4]


def test_recurrence_weekdays_only_valid_for_weekly():
    with pytest.raises(ValidationError):
        RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, weekdays=[0])


def test_recurrence_day_of_month_only_valid_for_monthly():
    with pytest.raises(ValidationError):
        RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, day_of_month=15)


def test_recurrence_monthly_with_day_of_month():
    spec = RecurrenceSpec(frequency=RecurrenceFrequency.MONTHLY, day_of_month=15)
    assert spec.day_of_month == 15


def test_recurrence_interval_must_be_positive():
    with pytest.raises(ValidationError):
        RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, interval=0)


def test_recurrence_cannot_specify_both_end_date_and_count():
    with pytest.raises(ValidationError):
        RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, end_date=date(2024, 12, 31), count=10)


def test_recurrence_attached_to_task_is_model_only():
    spec = RecurrenceSpec(frequency=RecurrenceFrequency.DAILY)
    task = _task(recurrence=spec)
    assert task.recurrence == spec
    # No expansion field/behavior exists on Task; recurrence stays a rule.
    assert not hasattr(task, "occurrences")


# -----------------------------------------------------------------------------
# LocalTimeWindow
# -----------------------------------------------------------------------------


def test_local_time_window_end_after_start():
    window = LocalTimeWindow(start_minute=540, end_minute=600)
    assert window.start_minute == 540


def test_local_time_window_rejects_end_before_start():
    with pytest.raises(ValidationError):
        LocalTimeWindow(start_minute=600, end_minute=540)


def test_local_time_window_allows_1440_as_end():
    window = LocalTimeWindow(start_minute=1380, end_minute=1440)
    assert window.end_minute == 1440


# -----------------------------------------------------------------------------
# FixedBlock vs ScheduledTask distinction
# -----------------------------------------------------------------------------


def test_fixed_block_requires_aware_instants():
    with pytest.raises(ValidationError):
        FixedBlock(
            label="Sleep",
            planned_date=date(2024, 6, 1),
            timezone="UTC",
            planned_start=datetime(2024, 6, 1, 0, 0),  # naive
            planned_end=datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc),
        )


def test_fixed_block_end_after_start():
    with pytest.raises(ValidationError):
        FixedBlock(
            label="Sleep",
            planned_date=date(2024, 6, 1),
            timezone="UTC",
            planned_start=datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc),
            planned_end=datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc),
        )


def test_fixed_block_validates_timezone():
    with pytest.raises(ValidationError):
        FixedBlock(
            label="Sleep",
            planned_date=date(2024, 6, 1),
            timezone="Not/AZone",
            planned_start=datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc),
            planned_end=datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc),
        )


def test_scheduled_task_has_no_name_field():
    task = _task()
    placement = _placement(task.id)
    assert not hasattr(placement, "name")
    assert not hasattr(placement, "category")
    assert not hasattr(placement, "tags")


def test_scheduled_task_and_fixed_block_are_distinct_types():
    task = _task()
    placement = _placement(task.id)
    fixed = FixedBlock(
        label="Sleep",
        planned_date=date(2024, 6, 1),
        timezone="UTC",
        planned_start=datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    assert type(placement) is not type(fixed)
    assert not isinstance(fixed, ScheduledTask)
    assert not isinstance(placement, FixedBlock)


# -----------------------------------------------------------------------------
# Display projection
# -----------------------------------------------------------------------------


def test_project_scheduled_task_display_reads_through_registry():
    task = _task(name="Study Math", category="study", tags=["math"])
    placement = _placement(task.id)
    registry = TaskRegistry()
    registry.add(task)

    display = project_scheduled_task_display(placement, registry)

    assert display.name == "Study Math"
    assert display.category == "study"
    assert display.tags == ["math"]


def test_project_scheduled_task_display_missing_task_raises():
    placement = _placement(uuid.uuid4())
    registry = TaskRegistry()
    with pytest.raises(KeyError):
        project_scheduled_task_display(placement, registry)


# -----------------------------------------------------------------------------
# DaySchedule
# -----------------------------------------------------------------------------


def test_day_schedule_rejects_unknown_task_ids():
    with pytest.raises(ValidationError):
        DaySchedule(date=date(2024, 6, 1), timezone="UTC", task_ids=[uuid.uuid4()])


def test_day_schedule_accepts_registered_task_ids():
    task = _task()
    registry = TaskRegistry()
    registry.add(task)
    schedule = DaySchedule(date=date(2024, 6, 1), timezone="UTC", task_ids=[task.id], tasks=registry)
    assert schedule.tasks.get(task.id) is task


def test_day_schedule_validates_timezone():
    with pytest.raises(ValidationError):
        DaySchedule(date=date(2024, 6, 1), timezone="Not/AZone")


# -----------------------------------------------------------------------------
# DayScheduleOutput consistency
# -----------------------------------------------------------------------------


def test_day_schedule_output_total_score_must_match_placements():
    task = _task()
    registry = TaskRegistry()
    registry.add(task)
    placement = _placement(task.id, score=5.0)

    with pytest.raises(ValidationError):
        DayScheduleOutput(
            date=date(2024, 6, 1),
            timezone="UTC",
            tasks=registry,
            placements=[placement],
            total_score=999.0,
        )


def test_day_schedule_output_accepts_computed_total_score():
    task = _task()
    registry = TaskRegistry()
    registry.add(task)
    placement = _placement(task.id, score=5.0)

    output = DayScheduleOutput(
        date=date(2024, 6, 1),
        timezone="UTC",
        tasks=registry,
        placements=[placement],
        total_score=compute_total_score([placement]),
    )
    assert output.total_score == 5.0


def test_day_schedule_output_rejects_placement_with_unknown_task():
    placement = _placement(uuid.uuid4(), score=0.0)
    with pytest.raises(ValidationError):
        DayScheduleOutput(
            date=date(2024, 6, 1),
            timezone="UTC",
            placements=[placement],
            total_score=0.0,
        )


def test_day_schedule_output_rejects_task_both_scheduled_and_unscheduled():
    task = _task()
    registry = TaskRegistry()
    registry.add(task)
    placement = _placement(task.id, score=0.0)
    entry = UnscheduledEntry(
        task_id=task.id,
        reason_code=UnscheduledReasonCode.NO_VALID_SLOT,
        explanation="no valid slot found",
    )

    with pytest.raises(ValidationError):
        DayScheduleOutput(
            date=date(2024, 6, 1),
            timezone="UTC",
            tasks=registry,
            placements=[placement],
            unscheduled=[entry],
            total_score=0.0,
        )


def test_day_schedule_output_unscheduled_entry_needs_explanation():
    task = _task()
    registry = TaskRegistry()
    registry.add(task)
    with pytest.raises(ValidationError):
        UnscheduledEntry(task_id=task.id, reason_code=UnscheduledReasonCode.OTHER, explanation="")


# -----------------------------------------------------------------------------
# Project
# -----------------------------------------------------------------------------


def test_project_has_uuid_identity():
    project = Project(name="Capstone")
    assert isinstance(project.id, uuid.UUID)


def test_project_optional_user_id_and_description():
    project = Project(name="Capstone")
    assert project.user_id is None
    assert project.description is None


# -----------------------------------------------------------------------------
# Versioned JSON planning-document round trip
# -----------------------------------------------------------------------------


def test_planning_document_round_trip_preserves_ids_dates_and_metadata():
    task = _task(name="Study Math", tags=["math"], priority=8)
    registry = TaskRegistry()
    registry.add(task)
    schedule = DaySchedule(date=date(2024, 6, 1), timezone="America/New_York", task_ids=[task.id], tasks=registry)
    document = PlanningDocument(day_schedule=schedule)

    restored = PlanningDocument.model_validate_json(document.model_dump_json())

    assert restored.schema_version == document.schema_version
    assert restored.day_schedule.date == date(2024, 6, 1)
    assert restored.day_schedule.timezone == "America/New_York"
    assert restored.day_schedule.task_ids == [task.id]
    restored_task = restored.day_schedule.tasks.get(task.id)
    assert restored_task is not None
    assert restored_task.name == "Study Math"
    assert restored_task.tags == ["math"]
    assert restored_task.priority == 8
