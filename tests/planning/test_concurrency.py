"""Optimistic concurrency, local revisions, and soft deletion (Milestone 3).

Every stale write or delete -- of a task, project, fixed block, placement
range, preference layer, or execution -- must fail with a structured
conflict and leave the newer stored record exactly as it was; versions must
advance by one per logical mutation; failed compound operations must roll
back completely; and deletion must keep history as tombstones.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.errors import ExecutionDeletedError, ExecutionNotFoundError, ExecutionVersionConflictError
from app.execution.models import ExecutionStatus
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.errors import DuplicateEntityError, InvalidEntityError, VersionConflictError
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from tests.planning.conftest import FakeClock

MON = date(2024, 6, 3)


def make_task(name: str = "Task", **overrides) -> Task:
    return Task(**{"name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5, **overrides})


def make_block(day: date = MON, hour: int = 12, label: str = "Lunch") -> FixedBlock:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return FixedBlock(label=label, category="food", planned_date=day, timezone="UTC",
                      planned_start=start, planned_end=start + timedelta(hours=1))


def make_placement(task: Task, day: date = MON, hour: int = 9) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=task.id, planned_date=day, timezone="UTC",
                         planned_start=start, planned_end=start + timedelta(hours=1))


# -----------------------------------------------------------------------------
# Stale writes and deletes never overwrite newer data
# -----------------------------------------------------------------------------


def test_stale_task_update_and_delete_leave_the_newer_task_unchanged(planning_service: PlanningService) -> None:
    original = planning_service.save_task(make_task("Draft"))
    newer = planning_service.update_task(original.model_copy(update={"name": "Newer"}), expected_version=1)
    assert newer.version == 2

    with pytest.raises(VersionConflictError) as conflict:
        planning_service.update_task(original.model_copy(update={"name": "Stale"}), expected_version=1)
    assert (conflict.value.expected_version, conflict.value.current_version, conflict.value.deleted) == (1, 2, False)

    with pytest.raises(VersionConflictError):
        planning_service.delete_task(original.id, expected_version=1)
    assert planning_service.get_task(original.id) == newer


def test_update_after_someone_else_deleted_is_a_conflict_not_a_revival(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    planning_service.delete_task(task.id, expected_version=1)

    with pytest.raises(VersionConflictError) as conflict:
        planning_service.update_task(task.model_copy(update={"name": "Revived?"}), expected_version=1)
    assert conflict.value.deleted is True
    assert planning_service.get_task(task.id) is None


def test_stale_project_writes_are_rejected(planning_service: PlanningService) -> None:
    project = planning_service.save_project(Project(name="Thesis"))
    planning_service.save_project(project.model_copy(update={"name": "Thesis v2"}), expected_version=1)

    with pytest.raises(VersionConflictError):
        planning_service.save_project(project.model_copy(update={"name": "Stale"}), expected_version=1)
    with pytest.raises(VersionConflictError):
        planning_service.delete_project(project.id, expected_version=1)
    assert planning_service.get_project(project.id).name == "Thesis v2"


def test_stale_fixed_block_writes_are_rejected(planning_service: PlanningService) -> None:
    block = planning_service.save_fixed_block(make_block())
    moved = planning_service.save_fixed_block(block.model_copy(update={"label": "Brunch"}), expected_version=1)

    with pytest.raises(VersionConflictError):
        planning_service.save_fixed_block(block.model_copy(update={"label": "Stale"}), expected_version=1)
    with pytest.raises(VersionConflictError):
        planning_service.delete_fixed_block(block.id, expected_version=1)
    # A day-replacement that does not know about a stored block cannot delete it.
    with pytest.raises(VersionConflictError):
        planning_service.set_fixed_blocks_for_date(MON, [])
    assert planning_service.fixed_blocks_for_date(MON) == [moved]


def test_stale_placement_range_replacement_is_rejected(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    [first] = planning_service.replace_placements(MON, MON, [make_placement(task)]).placements
    newer = planning_service.replace_placements(
        MON, MON, [first.model_copy(update={"score": 5.0})], expected_versions={first.id: 1}
    ).placements

    with pytest.raises(VersionConflictError):
        planning_service.replace_placements(MON, MON, [make_placement(task, hour=15)], expected_versions={first.id: 1})
    assert planning_service.placements_for_date(MON) == newer


def test_stale_preference_writes_and_deletes_are_rejected(planning_service: PlanningService) -> None:
    created = planning_service.save_user_preferences(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY))
    newer = planning_service.save_user_preferences(
        PreferenceOverrides(optimizer_mode=OptimizerMode.PRECISE_GREEDY), expected_version=created.version
    )

    with pytest.raises(VersionConflictError):
        planning_service.save_user_preferences(PreferenceOverrides(), expected_version=created.version)
    with pytest.raises(VersionConflictError):
        planning_service.save_user_preferences(PreferenceOverrides())  # a create cannot replace an existing layer
    with pytest.raises(VersionConflictError):
        planning_service.delete_user_preferences(expected_version=created.version)
    assert planning_service.user_preferences() == newer


def test_versions_carried_in_models_cannot_bypass_the_precondition(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    planning_service.update_task(task.model_copy(update={"name": "B"}), expected_version=1)

    crafted = task.model_copy(update={"name": "Crafted", "version": 999})
    with pytest.raises(VersionConflictError):
        planning_service.update_task(crafted, expected_version=1)
    assert planning_service.get_task(task.id).name == "B"


def test_ownership_cannot_change_through_an_update(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    with pytest.raises(InvalidEntityError, match="owner"):
        planning_service.update_task(task.model_copy(update={"user_id": uuid.uuid4()}), expected_version=1)


# -----------------------------------------------------------------------------
# Versions, rollback, tombstones
# -----------------------------------------------------------------------------


def test_each_logical_task_mutation_advances_the_version_by_one(planning_service: PlanningService, clock: FakeClock) -> None:
    task = planning_service.save_task(make_task())
    clock.advance(5)
    unchanged = planning_service.update_task(task, expected_version=1)
    assert unchanged.version == 1 and unchanged.updated_at == task.updated_at

    edited = planning_service.update_task(task.model_copy(update={"priority": 9}), expected_version=1)
    assert (edited.version, edited.updated_at, edited.created_at) == (2, clock(), task.created_at)

    clock.advance(5)
    assert planning_service.delete_task(task.id, expected_version=2)
    [tombstone] = planning_service.list_tasks(include_deleted=True)
    assert (tombstone.version, tombstone.deleted_at, tombstone.priority) == (3, clock(), 9)


def test_a_conflict_in_a_batch_rolls_back_the_whole_batch(planning_service: PlanningService) -> None:
    first, second = planning_service.save_tasks([make_task("First"), make_task("Second")])
    planning_service.update_task(second.model_copy(update={"name": "Second v2"}), expected_version=1)

    with pytest.raises(VersionConflictError):
        planning_service.save_tasks(
            [first.model_copy(update={"name": "First edited"}), second.model_copy(update={"name": "stale"}), make_task("New")],
            expected_versions={first.id: 1, second.id: 1},
        )
    assert sorted(task.name for task in planning_service.list_tasks()) == ["First", "Second v2"]


def test_soft_deletion_keeps_history_and_tombstones_are_not_reusable(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    task = planning_service.save_task(make_task("Worked on"))
    [placement] = planning_service.replace_placements(MON, MON, [make_placement(task)]).placements
    execution = execution_service.get_or_create_canonical_execution(task, placement)
    execution_service.start(execution.id)

    assert planning_service.delete_task(task.id, expected_version=1)

    assert planning_service.get_task(task.id) is None
    assert planning_service.placements_for_date(MON) == []
    assert planning_service.list_placements(include_deleted=True)[0].deleted_at is not None  # a tombstone, not gone
    history = execution_service.get_execution(execution.id)
    assert history.status == ExecutionStatus.IN_PROGRESS and history.task_id == task.id
    with pytest.raises(DuplicateEntityError):
        planning_service.create_task(make_task("Reuse", id=task.id))


# -----------------------------------------------------------------------------
# Executions
# -----------------------------------------------------------------------------


@pytest.fixture
def execution(planning_service: PlanningService, execution_service: ExecutionService):
    task = planning_service.save_task(make_task())
    [placement] = planning_service.replace_placements(MON, MON, [make_placement(task)]).placements
    return execution_service.get_or_create_canonical_execution(task, placement)


def test_execution_versions_advance_once_per_logical_mutation_including_sessions(
    execution_service: ExecutionService, execution
) -> None:
    assert execution.version == 1
    started = execution_service.start(execution.id, expected_version=1)  # status + session + first start
    paused = execution_service.pause(execution.id, expected_version=started.version)  # status + session close
    resumed = execution_service.resume(execution.id, expected_version=paused.version)
    completed = execution_service.complete(execution.id, expected_version=resumed.version)
    rated = execution_service.record_feedback(execution.id, expected_version=completed.version, focus_rating=4)

    assert [e.version for e in (started, paused, resumed, completed, rated)] == [2, 3, 4, 5, 6]
    assert execution_service.get_execution(execution.id).version == 6
    assert len(execution_service.list_sessions(execution.id)) == 2


def test_stale_execution_writes_change_nothing(execution_service: ExecutionService, execution) -> None:
    started = execution_service.start(execution.id, expected_version=1)
    rated = execution_service.record_feedback(execution.id, expected_version=started.version, note="newer")

    with pytest.raises(ExecutionVersionConflictError):
        execution_service.record_feedback(execution.id, expected_version=started.version, note="stale")
    with pytest.raises(ExecutionVersionConflictError):
        execution_service.pause(execution.id, expected_version=started.version)
    with pytest.raises(ExecutionVersionConflictError):
        execution_service.delete_execution(execution.id, expected_version=started.version)

    stored = execution_service.get_execution(execution.id)
    assert stored == rated
    assert execution_service.list_sessions(execution.id)[0].ended_at is None  # the stale pause closed nothing


def test_deleting_an_execution_is_a_tombstone(execution_service: ExecutionService, execution, planning_service) -> None:
    started = execution_service.start(execution.id, expected_version=1)
    execution_service.delete_execution(execution.id, expected_version=started.version)

    with pytest.raises(ExecutionNotFoundError):
        execution_service.get_execution(execution.id)
    assert execution_service.list_executions() == []
    assert execution_service.find_execution_for_placement(execution.scheduled_task_id) is None
    task = planning_service.get_task(execution.task_id)
    [placement] = planning_service.placements_for_date(MON)
    with pytest.raises(ExecutionDeletedError):  # neither revived nor silently duplicated
        execution_service.get_or_create_canonical_execution(task, placement)
