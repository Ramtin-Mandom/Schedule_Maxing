"""Tests for app/ui/planning_controller.py: the shared, Tk-free state
boundary for Day/Week/Month planning. No display access is required --
this exercises the controller's own state management directly, backed by a
PlanningService on a temporary database file (never the real data dir).
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import FixedBlock, Task
from app.planning.preferences import PreferenceOverrides, RewardPreferencesOverride
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus
from app.ui.planning_controller import PlanningController


def open_controller(db_path: Path, project_root: Path) -> tuple[PlanningController, object]:
    connection = get_connection(db_path)
    # An empty project_root (no config/ directory) so tests are isolated
    # from this repo's real config/task_preference.yaml.
    controller = PlanningController(
        service=PlanningService(PlanningRepository(connection)), timezone="UTC", project_root=str(project_root)
    )
    return controller, connection


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "planning.db"


@pytest.fixture
def controller(db_path, tmp_path):
    controller, connection = open_controller(db_path, tmp_path)
    try:
        yield controller
    finally:
        connection.close()


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


# -----------------------------------------------------------------------------
# Persistence delegation (Milestone 2)
# -----------------------------------------------------------------------------


def _generated_week(controller: PlanningController, task: Task):
    controller.add_or_update_task(task)
    allocation = controller.allocate_week(date(2024, 6, 3)).value
    assigned_date = allocation.assignments[task.id]
    result = controller.generate_day(assigned_date)
    assert result.ok, result.error
    return assigned_date, result.value


def test_default_controller_still_works_on_a_private_in_memory_store(tmp_path) -> None:
    controller = PlanningController(timezone="UTC", project_root=str(tmp_path))
    try:
        task = make_task()
        assert controller.add_or_update_task(task).ok
        assert [t.id for t in controller.list_tasks().value] == [task.id]
    finally:
        controller.close()


def test_controller_state_is_persisted_and_survives_reopen(db_path, tmp_path) -> None:
    first, connection = open_controller(db_path, tmp_path)
    task = make_task(duration=60)
    assigned_date, output = _generated_week(first, task)
    block = FixedBlock(
        label="Gym", planned_date=date(2024, 6, 9), timezone="UTC",
        planned_start=datetime(2024, 6, 9, 18, tzinfo=timezone.utc), planned_end=datetime(2024, 6, 9, 19, tzinfo=timezone.utc),
    )
    first.set_fixed_blocks(date(2024, 6, 9), [block])
    connection.close()

    second, connection = open_controller(db_path, tmp_path)
    try:
        assert second.get_task(task.id).value == task
        assert second.get_fixed_blocks(date(2024, 6, 9)).value == [block]
        assert second.get_placements(assigned_date).value == output.placements
        # Render state is not persisted: a reopened controller has no allocation.
        assert second.current_allocation().value is None
    finally:
        connection.close()


def test_regenerating_after_reopen_reuses_stored_placement_ids(db_path, tmp_path) -> None:
    first, connection = open_controller(db_path, tmp_path)
    task = make_task(duration=60)
    assigned_date, output = _generated_week(first, task)
    execution = ExecutionService(ExecutionRepository(connection)).get_or_create_canonical_execution(
        task, output.placements[0]
    )
    connection.close()

    second, connection = open_controller(db_path, tmp_path)
    try:
        second.allocate_week(date(2024, 6, 3))
        regenerated = second.generate_day(assigned_date).value
        assert [p.id for p in regenerated.placements] == [p.id for p in output.placements]
        again = ExecutionService(ExecutionRepository(connection)).get_or_create_canonical_execution(
            task, regenerated.placements[0]
        )
        assert again.id == execution.id
    finally:
        connection.close()


def test_failed_task_write_changes_nothing_and_invalidates_nothing(controller: PlanningController) -> None:
    task = make_task(duration=60)
    assigned_date, _ = _generated_week(controller, task)

    result = controller.add_or_update_task(Task(
        name="Broken", category="study", estimated_duration_minutes=30, priority=5, dependency_ids=[uuid.uuid4()],
    ))

    assert not result.ok
    assert "not persisted" in result.error
    assert [t.id for t in controller.list_tasks().value] == [task.id]
    assert controller.day_state(assigned_date).value.status == DayResultStatus.GENERATED
    assert controller.current_allocation().value is not None


def test_failed_placement_save_leaves_day_state_and_stored_placements_unchanged(
    controller: PlanningController, monkeypatch
) -> None:
    task = make_task(duration=60)
    assigned_date, output = _generated_week(controller, task)
    controller.add_or_update_task(make_task("Another", duration=30))  # -> STALE
    controller.allocate_week(date(2024, 6, 3))

    def fail(*args, **kwargs):
        raise RuntimeError("database is locked (injected)")

    monkeypatch.setattr(controller._service, "replace_placements", fail)
    result = controller.generate_day(assigned_date)

    assert not result.ok
    assert "injected" in result.error
    state = controller.day_state(assigned_date).value
    assert state.status == DayResultStatus.STALE
    assert state.result == output
    monkeypatch.undo()
    assert controller.get_placements(assigned_date).value == output.placements


def test_returned_models_are_snapshots(controller: PlanningController) -> None:
    task = make_task("Original")
    saved = controller.add_or_update_task(task).value

    saved.name = "mutated"
    controller.get_task(task.id).value.priority = 10
    controller.list_tasks().value[0].tags.append("mutated")
    task.name = "mutated input"

    stored = controller.get_task(task.id).value
    assert (stored.name, stored.priority, stored.tags) == ("Original", 5, [])


def test_deleting_a_task_others_depend_on_is_a_structured_failure(controller: PlanningController) -> None:
    dependency = make_task("Dependency")
    dependent = Task(name="Dependent", category="study", estimated_duration_minutes=30, priority=5,
                     dependency_ids=[dependency.id])
    assert controller.add_or_update_tasks([dependent, dependency]).ok

    result = controller.remove_task(dependency.id)

    assert not result.ok
    assert "depend" in result.error
    assert controller.get_task(dependency.id).value is not None


def test_allocation_only_considers_tasks_eligible_for_the_range(controller: PlanningController) -> None:
    this_week = make_task("This week")
    next_week = Task(name="Next week", category="study", estimated_duration_minutes=60, priority=5,
                     required=True, required_date=date(2024, 6, 12))
    controller.add_or_update_tasks([this_week, next_week])

    allocation = controller.allocate_week(date(2024, 6, 3)).value

    assert this_week.id in allocation.assignments
    assert all(entry.task_id != next_week.id for entry in allocation.unallocated)
    assert controller.allocate_week(date(2024, 6, 10)).value.assignments[next_week.id] == date(2024, 6, 12)


def test_controller_can_be_used_from_background_threads(controller: PlanningController) -> None:
    errors: list[str] = []

    def worker(index: int) -> None:
        for attempt in range(5):
            result = controller.add_or_update_task(make_task(f"T{index}-{attempt}"))
            if not result.ok:
                errors.append(result.error)
            controller.list_tasks()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(controller.list_tasks().value) == 20


# -----------------------------------------------------------------------------
# Range scheduling and restart (Milestone 2 desktop wiring)
# -----------------------------------------------------------------------------


def test_failed_schedule_range_adopts_nothing(controller: PlanningController, monkeypatch) -> None:
    from app.planning.application import RangeScope

    task = make_task(duration=60)
    controller.add_or_update_task(task)
    first = controller.schedule_range(date(2024, 6, 3), date(2024, 6, 9), scope=RangeScope.ELIGIBLE)
    assert first.ok, first.error
    allocation_id = controller.current_allocation().value.id
    assigned = first.value.allocation.assignments[task.id]
    saved = controller.get_placements(assigned).value

    def fail(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(controller._service, "replace_placements", fail)
    result = controller.schedule_range(date(2024, 6, 3), date(2024, 6, 9), scope=RangeScope.ELIGIBLE)

    assert not result.ok
    assert controller.current_allocation().value.id == allocation_id
    assert controller.day_state(assigned).value.status == DayResultStatus.GENERATED
    monkeypatch.undo()
    assert controller.get_placements(assigned).value == saved


def test_saved_placements_without_session_state_are_reported_stale(db_path, tmp_path) -> None:
    first, connection = open_controller(db_path, tmp_path)
    task = make_task(duration=60)
    assigned_date, output = _generated_week(first, task)
    connection.close()

    second, connection = open_controller(db_path, tmp_path)
    try:
        state = second.day_state(assigned_date).value
        assert state.status == DayResultStatus.STALE
        assert state.result.placements == output.placements
        assert second.day_state(date(2024, 6, 20)).value.status == DayResultStatus.ALLOCATED
    finally:
        connection.close()
