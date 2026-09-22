"""Tests for app/planning/allocation.py: week/month calendar coverage,
capacity/required/deadline/preference-driven ranking, cross-date
dependencies, and the "never calls the Day Scheduler" guarantee.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.planning.allocation import (
    AllocationReasonCode,
    allocate_tasks,
    month_dates,
    week_dates,
)
from app.planning.models import FixedBlock, Task, TaskRegistry
from app.planning.preferences import PreferenceOverrides, resolve_day_preferences

TZ = "UTC"


def make_task(
    name="Task", *, duration=60, priority=5, category="study", required=False, dependency_ids=None,
    deadline=None, required_date=None, preferred_dates=None, task_id=None,
):
    kwargs = dict(
        name=name, category=category, estimated_duration_minutes=duration, priority=priority,
        required=required, dependency_ids=dependency_ids or [], deadline=deadline,
        required_date=required_date, preferred_dates=preferred_dates or [],
    )
    if task_id is not None:
        kwargs["id"] = task_id
    return Task(**kwargs)


def registry_of(*tasks: Task) -> TaskRegistry:
    reg = TaskRegistry()
    for task in tasks:
        reg.add(task)
    return reg


def prefs_for(dates: list[date], **overrides) -> dict[date, "DayPreferences"]:  # noqa: F821
    return {d: resolve_day_preferences(date=d, timezone=TZ, **overrides) for d in dates}


# -----------------------------------------------------------------------------
# Calendar coverage
# -----------------------------------------------------------------------------


def test_week_dates_gives_seven_actual_dates():
    dates = week_dates(date(2024, 6, 3))
    assert dates == [date(2024, 6, 3) + timedelta(days=i) for i in range(7)]
    assert len(dates) == 7


def test_month_dates_leap_february():
    dates = month_dates(2024, 2)
    assert len(dates) == 29
    assert dates[0] == date(2024, 2, 1)
    assert dates[-1] == date(2024, 2, 29)


def test_month_dates_non_leap_february():
    dates = month_dates(2023, 2)
    assert len(dates) == 28


def test_month_dates_31_day_month():
    assert len(month_dates(2024, 1)) == 31


def test_week_spanning_year_boundary():
    dates = week_dates(date(2023, 12, 29))
    assert dates[-1] == date(2024, 1, 4)
    assert len(dates) == 7


def test_allocation_result_never_contains_minute_level_placements():
    """AllocationResult structurally has no placements/start/end fields at all."""
    dates = week_dates(date(2024, 6, 3))
    task = make_task(duration=60)
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert not hasattr(result, "placements")
    assert set(type(result).model_fields) == {
        "id", "created_at", "start_date", "end_date", "assignments", "capacity_remaining", "unallocated", "diagnostics",
    }


# -----------------------------------------------------------------------------
# Allocation never calls the Day Scheduler
# -----------------------------------------------------------------------------


def test_allocation_never_invokes_the_day_optimizer(monkeypatch):
    import app.optimizer as optimizer_module

    def _fail(*args, **kwargs):
        raise AssertionError("allocate_tasks must never call generate_day_schedule")

    monkeypatch.setattr(optimizer_module, "generate_day_schedule", _fail)

    dates = week_dates(date(2024, 6, 3))
    tasks = [make_task(f"T{i}", duration=60) for i in range(5)]
    allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(*tasks), task_ids=[t.id for t in tasks],
        preferences_by_date=prefs_for(dates),
    )
    # No assertion error raised means generate_day_schedule was never called.


# -----------------------------------------------------------------------------
# Capacity, fixed occupancy
# -----------------------------------------------------------------------------


def test_fixed_occupancy_reduces_capacity():
    day = date(2024, 6, 3)
    fixed = FixedBlock(
        label="Sleep", planned_date=day, timezone=TZ,
        planned_start=datetime(2024, 6, 3, 0, 0, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, 8, 0, tzinfo=timezone.utc),
    )
    task = make_task(duration=60)
    result = allocate_tasks(
        start_date=day, end_date=day, tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
        fixed_blocks_by_date={day: [fixed]},
    )
    assert result.capacity_remaining[day] == 1440 - 480 - 60


def test_overlapping_fixed_blocks_raise():
    day = date(2024, 6, 3)
    a = FixedBlock(
        label="A", planned_date=day, timezone=TZ,
        planned_start=datetime(2024, 6, 3, 0, 0, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, 2, 0, tzinfo=timezone.utc),
    )
    b = FixedBlock(
        label="B", planned_date=day, timezone=TZ,
        planned_start=datetime(2024, 6, 3, 1, 0, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, 3, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError):
        allocate_tasks(
            start_date=day, end_date=day, tasks=TaskRegistry(), task_ids=[],
            preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
            fixed_blocks_by_date={day: [a, b]},
        )


def test_capacity_exceeded_is_reported_not_raised():
    day = date(2024, 6, 3)
    big_a = make_task("A", duration=1440, required=True)
    big_b = make_task("B", duration=1440, required=True)
    result = allocate_tasks(
        start_date=day, end_date=day, tasks=registry_of(big_a, big_b), task_ids=[big_a.id, big_b.id],
        preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
    )
    # Exactly one of the two (whichever the deterministic priority/id
    # tie-break picks first) fits the single day; the other cannot.
    assert len(result.assignments) == 1
    assert len(result.unallocated) == 1
    loser = result.unallocated[0]
    assert loser.task_id in {big_a.id, big_b.id}
    assert loser.reason_code == AllocationReasonCode.CAPACITY_EXCEEDED
    assert loser.required is True


# -----------------------------------------------------------------------------
# Required dates, priorities, preferences, deadlines
# -----------------------------------------------------------------------------


def test_required_date_pins_task_to_that_date():
    dates = week_dates(date(2024, 6, 3))
    pinned = dates[3]
    task = make_task(duration=60, required=True, required_date=pinned)
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert result.assignments[task.id] == pinned


def test_required_date_outside_range_is_proven_infeasible():
    dates = week_dates(date(2024, 6, 3))
    outside = dates[-1] + timedelta(days=10)
    task = make_task(duration=60, required=True, required_date=outside)
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    entry = result.unallocated[0]
    assert entry.reason_code == AllocationReasonCode.REQUIRED_DATE_CONFLICT
    assert entry.proven_infeasible is True


def test_deadline_restricts_candidate_dates():
    dates = week_dates(date(2024, 6, 3))
    deadline = datetime.combine(dates[2], datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)
    task = make_task(duration=60, deadline=deadline)
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert result.assignments[task.id] <= dates[2]


def test_preferred_dates_are_favored_in_ranking():
    dates = week_dates(date(2024, 6, 3))
    preferred = dates[4]
    task = make_task(duration=60, preferred_dates=[preferred])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert result.assignments[task.id] == preferred


def test_higher_category_multiplier_date_is_favored():
    dates = week_dates(date(2024, 6, 3))
    boosted_date = dates[5]
    preferences = prefs_for(dates)
    preferences[boosted_date] = resolve_day_preferences(
        date=boosted_date, timezone=TZ, date_overrides=PreferenceOverrides(category_multipliers={"study": 5.0})
    )
    task = make_task(duration=60, category="study")
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=preferences,
    )
    assert result.assignments[task.id] == boosted_date


def test_earliest_date_is_the_final_tiebreak():
    dates = week_dates(date(2024, 6, 3))
    task = make_task(duration=60)
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert result.assignments[task.id] == dates[0]


# -----------------------------------------------------------------------------
# Cross-date dependencies
# -----------------------------------------------------------------------------


def test_dependent_assigned_no_earlier_than_prerequisite():
    dates = week_dates(date(2024, 6, 3))
    prereq = make_task("Prereq", duration=1440)  # fills a whole day, forcing separation
    dependent = make_task("Dependent", duration=60, dependency_ids=[prereq.id])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(prereq, dependent), task_ids=[dependent.id, prereq.id],
        preferences_by_date=prefs_for(dates),
    )
    assert result.assignments[dependent.id] >= result.assignments[prereq.id]


def test_same_date_dependency_allowed():
    dates = week_dates(date(2024, 6, 3))
    prereq = make_task("Prereq", duration=30)
    dependent = make_task("Dependent", duration=30, dependency_ids=[prereq.id])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[0], tasks=registry_of(prereq, dependent), task_ids=[dependent.id, prereq.id],
        preferences_by_date={dates[0]: resolve_day_preferences(date=dates[0], timezone=TZ)},
    )
    assert result.assignments[dependent.id] == result.assignments[prereq.id] == dates[0]


def test_dependency_cycle_raises():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    task_a = make_task("A", dependency_ids=[id_b], task_id=id_a)
    task_b = make_task("B", dependency_ids=[id_a], task_id=id_b)
    day = date(2024, 6, 3)
    with pytest.raises(ValueError):
        allocate_tasks(
            start_date=day, end_date=day, tasks=registry_of(task_a, task_b), task_ids=[task_a.id, task_b.id],
            preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
        )


def test_external_dependency_context_unblocks_a_task():
    dates = week_dates(date(2024, 6, 3))
    external_id = uuid.uuid4()
    task = make_task("Dependent", duration=60, dependency_ids=[external_id])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
        external_dependency_dates={external_id: dates[2]},
    )
    assert result.assignments[task.id] >= dates[2]


def test_unresolved_dependency_blocks_task_not_ignored():
    dates = week_dates(date(2024, 6, 3))
    unknown_id = uuid.uuid4()
    task = make_task("Blocked", duration=60, dependency_ids=[unknown_id])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(task), task_ids=[task.id],
        preferences_by_date=prefs_for(dates),
    )
    assert task.id not in result.assignments
    entry = result.unallocated[0]
    assert entry.reason_code == AllocationReasonCode.DEPENDENCY_UNRESOLVED


def test_dependent_of_unallocated_local_task_is_blocked():
    dates = week_dates(date(2024, 6, 3))
    too_big = make_task("TooBig", duration=100_000, required=True)  # never fits
    dependent = make_task("Dependent", duration=30, dependency_ids=[too_big.id])
    result = allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=registry_of(too_big, dependent),
        task_ids=[dependent.id, too_big.id], preferences_by_date=prefs_for(dates),
    )
    entries_by_id = {e.task_id: e for e in result.unallocated}
    assert too_big.id in entries_by_id
    assert dependent.id in entries_by_id
    assert entries_by_id[dependent.id].reason_code == AllocationReasonCode.BLOCKED_BY_UNALLOCATED_DEPENDENCY


# -----------------------------------------------------------------------------
# Required vs optional distinction
# -----------------------------------------------------------------------------


def test_required_and_optional_unallocated_are_distinguishable():
    day = date(2024, 6, 3)
    required_big = make_task("Required", duration=1440, required=True)
    optional_extra = make_task("Optional", duration=60)
    result = allocate_tasks(
        start_date=day, end_date=day, tasks=registry_of(required_big, optional_extra),
        task_ids=[required_big.id, optional_extra.id],
        preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
    )
    entries_by_id = {e.task_id: e for e in result.unallocated}
    assert entries_by_id[optional_extra.id].required is False


def test_required_tasks_are_processed_before_optional_for_the_same_capacity():
    day = date(2024, 6, 3)
    optional_high_priority = make_task("OptionalHighPriority", duration=1440, priority=10, required=False)
    required_low_priority = make_task("RequiredLowPriority", duration=1440, priority=1, required=True)
    result = allocate_tasks(
        start_date=day, end_date=day, tasks=registry_of(optional_high_priority, required_low_priority),
        task_ids=[optional_high_priority.id, required_low_priority.id],
        preferences_by_date={day: resolve_day_preferences(date=day, timezone=TZ)},
    )
    assert required_low_priority.id in result.assignments
    assert optional_high_priority.id not in result.assignments
