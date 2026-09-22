"""Tests for app/ui/planning_controller.py: the shared, Tk-free state
boundary for Day/Week/Month planning. No display access is required --
this exercises the controller's own state management directly.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.planning.models import Task
from app.planning.preferences import PreferenceOverrides, RewardPreferencesOverride
from app.planning.service import DayResultStatus
from app.ui.planning_controller import PlanningController


@pytest.fixture
def controller(tmp_path) -> PlanningController:
    # An empty project_root (no config/ directory) so tests are isolated
    # from this repo's real config/task_preference.yaml.
    return PlanningController(timezone="UTC", project_root=str(tmp_path))


def make_task(name="Task", *, duration=60, priority=5, category="study") -> Task:
    return Task(name=name, category=category, estimated_duration_minutes=duration, priority=priority)


# -----------------------------------------------------------------------------
# Shared task identity across views
# -----------------------------------------------------------------------------


def test_added_task_is_immediately_visible_via_get_and_list(controller: PlanningController):
    task = make_task()
    controller.add_or_update_task(task)

    assert controller.get_task(task.id).value.id == task.id
    assert task in controller.list_tasks().value


def test_remove_task_clears_it(controller: PlanningController):
    task = make_task()
    controller.add_or_update_task(task)
    controller.remove_task(task.id)

    assert controller.get_task(task.id).value is None


# -----------------------------------------------------------------------------
# Preferences: independence, layering
# -----------------------------------------------------------------------------


def test_date_override_does_not_affect_other_dates(controller: PlanningController):
    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    controller.set_date_overrides(day1, PreferenceOverrides(category_multipliers={"study": 3.0}))

    prefs1 = controller.resolve_preferences(day1).value
    prefs2 = controller.resolve_preferences(day2).value

    assert prefs1.effective_category_multiplier("study") == 3.0
    assert prefs2.effective_category_multiplier("study") == 1.0


def test_user_overrides_apply_to_every_date_unless_shadowed(controller: PlanningController):
    controller.set_user_overrides(PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=99.0)))
    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    controller.set_date_overrides(day2, PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=1.0)))

    assert controller.resolve_preferences(day1).value.reward.weight_importance == 99.0
    assert controller.resolve_preferences(day2).value.reward.weight_importance == 1.0  # date overrides user


# -----------------------------------------------------------------------------
# Allocation and generation share identity
# -----------------------------------------------------------------------------


def test_allocate_week_then_generate_day_uses_the_same_task_identity(controller: PlanningController):
    task = make_task(duration=60)
    controller.add_or_update_task(task)

    allocation_result = controller.allocate_week(date(2024, 6, 3))
    assert allocation_result.ok
    assigned_date = allocation_result.value.assignments[task.id]

    generate_result = controller.generate_day(assigned_date)
    assert generate_result.ok
    assert generate_result.value.placements[0].task_id == task.id


def test_generate_day_without_allocation_fails_clearly(controller: PlanningController):
    result = controller.generate_day(date(2024, 6, 3))
    assert not result.ok
    assert "Allocate" in result.error


def test_allocation_level_mandatory_failure_is_visible_in_allocation_result(controller: PlanningController):
    """A task too long for any single day (over the whole week's per-day
    capacity) fails at the allocation stage -- it never gets assigned a
    date at all, so generating that date later simply finds nothing to do
    for it (not a day-scheduler error)."""
    too_long_required = Task(
        name="TooLong", category="study", estimated_duration_minutes=2000, priority=5, required=True,
    )
    controller.add_or_update_task(too_long_required)

    allocation_result = controller.allocate_week(date(2024, 6, 3))

    assert allocation_result.ok
    unallocated = allocation_result.value.unallocated
    assert any(entry.task_id == too_long_required.id and entry.required for entry in unallocated)
    assert too_long_required.id not in allocation_result.value.assignments


def test_generation_level_mandatory_failure_surfaces_through_controller_result(controller: PlanningController):
    """A required task that is coarse-feasible at allocation (fits the
    day's aggregate free capacity) but cannot actually fit any single free
    interval once a fixed block fragments the day must still fail loudly
    at generation time, through the same ControllerResult channel."""
    from datetime import datetime, timezone

    from app.planning.models import FixedBlock

    day = date(2024, 6, 3)
    required_task = Task(
        name="Fragmented", category="study", estimated_duration_minutes=1000, priority=5, required=True,
    )
    controller.add_or_update_task(required_task)
    blocker = FixedBlock(
        label="Blocker", planned_date=day, timezone="UTC",
        planned_start=datetime(2024, 6, 3, 11, 40, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, 13, 20, tzinfo=timezone.utc),
    )
    controller.set_fixed_blocks(day, [blocker])

    # A single-day range so the task cannot be ranked onto some other,
    # less-loaded day of a wider week -- it must land on `day` itself.
    allocation_result = controller.allocate_range(day, day)
    assert allocation_result.value.assignments.get(required_task.id) == day  # coarse-feasible: aggregate capacity is enough

    result = controller.generate_day(day)

    assert not result.ok
    assert "Could not generate" in result.error


# -----------------------------------------------------------------------------
# Staleness / invalidation
# -----------------------------------------------------------------------------


def test_editing_a_task_marks_generated_days_stale(controller: PlanningController):
    task = make_task(duration=60)
    controller.add_or_update_task(task)
    allocation = controller.allocate_week(date(2024, 6, 3)).value
    assigned_date = allocation.assignments[task.id]
    controller.generate_day(assigned_date)

    assert controller.day_state(assigned_date).value.status == DayResultStatus.GENERATED

    # Editing a task invalidates the generated result and clears the
    # current allocation (must be explicitly re-run, never silently redone).
    controller.add_or_update_task(make_task("Another", duration=30))

    assert controller.day_state(assigned_date).value.status == DayResultStatus.STALE
    assert controller.current_allocation().value is None


def test_fixed_block_edit_invalidates_generated_days(controller: PlanningController):
    from app.planning.models import FixedBlock
    from datetime import datetime, timezone

    task = make_task(duration=60)
    controller.add_or_update_task(task)
    allocation = controller.allocate_week(date(2024, 6, 3)).value
    assigned_date = allocation.assignments[task.id]
    controller.generate_day(assigned_date)

    block = FixedBlock(
        label="Sleep", planned_date=assigned_date, timezone="UTC",
        planned_start=datetime.combine(assigned_date, datetime.min.time(), tzinfo=timezone.utc),
        planned_end=datetime.combine(assigned_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=8),
    )
    controller.set_fixed_blocks(assigned_date, [block])

    assert controller.day_state(assigned_date).value.status == DayResultStatus.STALE


def test_reallocating_after_invalidation_produces_a_fresh_allocation(controller: PlanningController):
    task = make_task(duration=60)
    controller.add_or_update_task(task)
    first_allocation = controller.allocate_week(date(2024, 6, 3)).value

    controller.add_or_update_task(make_task("Another", duration=30))
    second_allocation = controller.allocate_week(date(2024, 6, 3)).value

    assert second_allocation.id != first_allocation.id


# -----------------------------------------------------------------------------
# Allocation never regresses to auto-generating every date
# -----------------------------------------------------------------------------


def test_allocate_week_does_not_generate_any_day(controller: PlanningController, monkeypatch):
    import app.planning.service as service_module

    def _fail(*args, **kwargs):
        raise AssertionError("allocate_week must never call generate_day_schedule")

    monkeypatch.setattr(service_module, "generate_day_schedule", _fail)

    task = make_task(duration=60)
    controller.add_or_update_task(task)
    result = controller.allocate_week(date(2024, 6, 3))

    assert result.ok
    for day in [date(2024, 6, 3 + offset) for offset in range(7)]:
        assert controller.day_state(day).value.status == DayResultStatus.ALLOCATED
