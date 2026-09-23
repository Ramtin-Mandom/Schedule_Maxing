"""Tests for app/planning/application.py (PlanningService): the persistence-backed
planning boundary's business rules -- create/update/save semantics and audit
fields, reference validation, atomic bulk writes, the documented
deletion/history policy, scoped placement replacement, fixed-block scope,
date-range loading and eligibility, and snapshot isolation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.models import ExecutionStatus
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.errors import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    InvalidEntityError,
    InvalidReferenceError,
    ScopeError,
)
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from tests.planning.conftest import FakeClock

MON, TUE, WED = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)


def make_task(name: str = "Task", **overrides) -> Task:
    defaults = dict(name=name, category="study", estimated_duration_minutes=60, priority=5)
    defaults.update(overrides)
    return Task(**defaults)


def make_placement(task: Task, day: date, hour: int = 9, **overrides) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    defaults = dict(
        task_id=task.id, planned_date=day, timezone="UTC", planned_start=start, planned_end=start + timedelta(hours=1)
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


def make_block(day: date, hour: int = 12, label: str = "Lunch") -> FixedBlock:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return FixedBlock(label=label, planned_date=day, timezone="UTC", planned_start=start, planned_end=start + timedelta(hours=1))


# -----------------------------------------------------------------------------
# Task CRUD and audit fields
# -----------------------------------------------------------------------------


def test_create_stores_exactly_as_given_and_rejects_duplicates(planning_service: PlanningService) -> None:
    task = make_task(version=3, created_at=datetime(2023, 1, 1, tzinfo=timezone.utc))

    created = planning_service.create_task(task)

    assert created == task
    with pytest.raises(DuplicateEntityError):
        planning_service.create_task(task)


def test_update_requires_existing_task(planning_service: PlanningService) -> None:
    with pytest.raises(EntityNotFoundError):
        planning_service.update_task(make_task(), expected_version=1)


def test_changed_save_bumps_version_and_updated_at_but_keeps_created_at(
    planning_service: PlanningService, clock: FakeClock
) -> None:
    original = planning_service.save_task(make_task("Draft"))
    clock.advance(30)

    updated = planning_service.save_task(
        original.model_copy(update={"name": "Final", "created_at": clock()}), expected_version=original.version
    )

    assert updated.name == "Final"
    assert updated.version == original.version + 1
    assert updated.created_at == original.created_at
    assert updated.updated_at == clock()


def test_unchanged_save_is_a_no_op(planning_service: PlanningService, clock: FakeClock) -> None:
    original = planning_service.save_task(make_task())
    clock.advance(5)

    again = planning_service.save_task(original, expected_version=original.version)

    assert again == original
    assert again.version == original.version


def test_version_carried_by_the_model_is_never_trusted(planning_service: PlanningService) -> None:
    stored = planning_service.save_task(make_task(version=5))
    edited = planning_service.save_task(stored.model_copy(update={"name": "edited", "version": 1}), expected_version=5)
    assert edited.version == 6  # never backwards...
    jumped = planning_service.save_task(edited.model_copy(update={"name": "again", "version": 99}), expected_version=6)
    assert jumped.version == 7  # ...and a crafted version cannot jump ahead either


def test_save_tasks_is_atomic_and_order_independent(planning_service: PlanningService) -> None:
    dependency = make_task("Dependency")
    dependent = make_task("Dependent", dependency_ids=[dependency.id])

    saved = planning_service.save_tasks([dependent, dependency])  # dependent first
    assert [task.id for task in saved] == [dependent.id, dependency.id]

    missing = uuid.uuid4()
    batch = [make_task("A"), make_task("B", dependency_ids=[missing]), make_task("C")]
    with pytest.raises(InvalidReferenceError) as info:
        planning_service.save_tasks(batch)
    assert info.value.missing_ids == [missing]
    assert {task.name for task in planning_service.list_tasks()} == {"Dependency", "Dependent"}


def test_multi_row_save_rolls_back_after_an_injected_failure(planning_service, planning_repository, monkeypatch) -> None:
    original_insert = planning_repository.insert_task
    calls = {"count": 0}

    def fail_on_third(task):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("disk full (injected)")
        original_insert(task)

    monkeypatch.setattr(planning_repository, "insert_task", fail_on_third)
    with pytest.raises(RuntimeError, match="injected"):
        planning_service.save_tasks([make_task("A"), make_task("B"), make_task("C")])

    assert planning_service.list_tasks() == []


def test_project_reference_must_be_persisted(planning_service: PlanningService) -> None:
    project = Project(name="Thesis")
    with pytest.raises(InvalidReferenceError):
        planning_service.save_task(make_task(project_id=project.id))

    planning_service.save_project(project)
    assert planning_service.save_task(make_task(project_id=project.id)).project_id == project.id


def test_snapshot_isolation(planning_service: PlanningService) -> None:
    task = make_task("Original", tags=["x"])
    saved = planning_service.save_task(task)

    task.name = "mutated input"
    saved.tags.append("mutated output")
    fetched = planning_service.get_task(task.id)
    fetched.priority = 10

    assert planning_service.get_task(task.id) == saved.model_copy(update={"tags": ["x"]})
    assert planning_service.get_task(task.id).name == "Original"
    assert planning_service.get_task(task.id).priority == 5


# -----------------------------------------------------------------------------
# Deletion policy
# -----------------------------------------------------------------------------


def test_deleting_a_dependency_of_a_surviving_task_is_refused(planning_service: PlanningService) -> None:
    dependency = make_task("Dependency")
    dependent = make_task("Dependent", dependency_ids=[dependency.id])
    planning_service.save_tasks([dependency, dependent])

    with pytest.raises(EntityInUseError) as info:
        planning_service.delete_task(dependency.id, expected_version=1)
    assert info.value.dependent_ids == [dependent.id]
    assert planning_service.get_task(dependency.id) is not None

    assert planning_service.delete_tasks({dependency.id: 1, dependent.id: 1}) == 2
    assert planning_service.list_tasks() == []


def test_deleting_a_dependent_removes_only_its_own_edges(planning_service: PlanningService) -> None:
    dependency = make_task("Dependency")
    dependent = make_task("Dependent", dependency_ids=[dependency.id])
    planning_service.save_tasks([dependency, dependent])

    assert planning_service.delete_task(dependent.id, expected_version=1) is True
    assert planning_service.get_task(dependency.id) is not None
    assert planning_service.delete_task(dependent.id, expected_version=1) is False  # already gone


def test_project_with_tasks_cannot_be_deleted(planning_service: PlanningService) -> None:
    project = planning_service.save_project(Project(name="P"))
    task = planning_service.save_task(make_task(project_id=project.id))

    with pytest.raises(EntityInUseError):
        planning_service.delete_project(project.id, expected_version=project.version)

    planning_service.delete_task(task.id, expected_version=task.version)
    assert planning_service.delete_project(project.id, expected_version=project.version) is True


def test_deleting_a_task_never_touches_its_execution_history(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    task = planning_service.save_task(make_task("Worked on"))
    placement = planning_service.replace_placements(MON, MON, [make_placement(task, MON)]).placements[0]
    execution = execution_service.get_or_create_canonical_execution(task, placement)
    execution_service.start(execution.id)
    execution_service.complete(execution.id)

    planning_service.delete_task(task.id, expected_version=task.version)

    assert planning_service.placements_for_date(MON) == []  # placements go with their task
    history = execution_service.get_execution(execution.id)  # history stays, identity intact
    assert history.status == ExecutionStatus.COMPLETED
    assert history.task_id == task.id and history.scheduled_task_id == placement.id
    assert history.task_name == "Worked on"
    assert len(execution_service.list_sessions(execution.id)) == 1


# -----------------------------------------------------------------------------
# Placement replacement
# -----------------------------------------------------------------------------


def test_replacement_is_scoped_to_its_date_range(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    planning_service.replace_placements(MON, WED, [make_placement(task, day) for day in (MON, TUE, WED)])
    wednesday = planning_service.placements_for_date(WED)

    new_tuesday = make_placement(task, TUE, hour=14)
    result = planning_service.replace_placements(MON, TUE, [new_tuesday])

    assert [p.id for p in result.placements] == [new_tuesday.id]
    assert planning_service.placements_for_date(MON) == []
    assert planning_service.placements_for_date(WED) == wednesday  # untouched
    assert len(result.removed_ids) == 2


def test_replacement_rejects_out_of_scope_placements(planning_service: PlanningService) -> None:
    task = planning_service.save_task(make_task())
    stored_wednesday = planning_service.replace_placements(WED, WED, [make_placement(task, WED)]).placements[0]

    with pytest.raises(ScopeError):
        planning_service.replace_placements(MON, MON, [make_placement(task, TUE)])
    with pytest.raises(ScopeError, match="will not move"):
        planning_service.replace_placements(MON, MON, [stored_wednesday.model_copy(update={"planned_date": MON})])
    with pytest.raises(ScopeError):
        planning_service.replace_placements(TUE, MON, [])

    assert planning_service.placements_for_date(WED) == [stored_wednesday]


def test_failed_replacement_leaves_every_placement_unchanged(
    planning_service: PlanningService, planning_repository, monkeypatch
) -> None:
    task = planning_service.save_task(make_task())
    original = [make_placement(task, MON, 8), make_placement(task, MON, 10)]
    before = planning_service.replace_placements(MON, MON, original).placements

    original_insert = planning_repository.insert_placement
    calls = {"count": 0}

    def fail_on_second(placement):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected")
        original_insert(placement)

    monkeypatch.setattr(planning_repository, "insert_placement", fail_on_second)
    with pytest.raises(RuntimeError, match="injected"):
        planning_service.replace_placements(MON, MON, [make_placement(task, MON, 13), make_placement(task, MON, 15)])

    assert planning_service.placements_for_date(MON) == before


def test_replacement_requires_persisted_tasks(planning_service: PlanningService) -> None:
    with pytest.raises(InvalidReferenceError):
        planning_service.replace_placements(MON, MON, [make_placement(make_task(), MON)])


def test_reused_placement_id_keeps_history_link_and_audit_fields(
    planning_service: PlanningService, execution_service: ExecutionService, clock: FakeClock
) -> None:
    task = planning_service.save_task(make_task())
    first = planning_service.replace_placements(MON, MON, [make_placement(task, MON)]).placements[0]
    execution = execution_service.get_or_create_canonical_execution(task, first)
    clock.advance(10)

    unchanged = planning_service.replace_placements(MON, MON, [first.model_copy(update={"created_at": clock()})])
    rescored = planning_service.replace_placements(MON, MON, [first.model_copy(update={"score": 99.0})])

    assert unchanged.placements == [first]
    assert unchanged.removed_ids == []
    [stored] = rescored.placements
    assert stored.id == first.id and stored.score == 99.0
    assert stored.version == first.version + 1 and stored.created_at == first.created_at
    assert execution_service.get_or_create_canonical_execution(task, stored).id == execution.id


def test_replacing_a_history_linked_placement_keeps_history_and_reports_it(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    task = planning_service.save_task(make_task())
    linked = planning_service.replace_placements(MON, MON, [make_placement(task, MON)]).placements[0]
    execution = execution_service.get_or_create_canonical_execution(task, linked)
    execution_service.start(execution.id)

    moved = make_placement(task, MON, hour=15)
    result = planning_service.replace_placements(MON, MON, [moved])

    assert result.removed_with_history_ids == [linked.id]
    history = execution_service.get_execution(execution.id)
    assert history.status == ExecutionStatus.IN_PROGRESS
    assert history.scheduled_task_id == linked.id
    assert history.canonical_planned_start == linked.planned_start  # snapshot of what was planned
    # The moved placement is a distinct placement with its own execution.
    assert execution_service.get_or_create_canonical_execution(task, moved).id != execution.id


# -----------------------------------------------------------------------------
# Fixed blocks
# -----------------------------------------------------------------------------


def test_set_fixed_blocks_for_date_replaces_only_that_date(planning_service: PlanningService) -> None:
    tuesday_block = planning_service.save_fixed_block(make_block(TUE))
    first = planning_service.set_fixed_blocks_for_date(MON, [make_block(MON, 8, "A"), make_block(MON, 12, "B")])

    result = planning_service.set_fixed_blocks_for_date(
        MON, [make_block(MON, 18, "C")], expected_versions={block.id: block.version for block in first}
    )

    assert [block.label for block in result] == ["C"]
    assert planning_service.fixed_blocks_for_date(TUE) == [tuesday_block]


def test_set_fixed_blocks_for_date_scope_checks(planning_service: PlanningService) -> None:
    tuesday_block = planning_service.save_fixed_block(make_block(TUE))

    with pytest.raises(ScopeError):
        planning_service.set_fixed_blocks_for_date(MON, [make_block(TUE)])
    with pytest.raises(ScopeError, match="will not move"):
        planning_service.set_fixed_blocks_for_date(MON, [tuesday_block.model_copy(update={"planned_date": MON})])
    block = make_block(MON)
    with pytest.raises(InvalidEntityError):
        planning_service.set_fixed_blocks_for_date(MON, [block, block])

    assert planning_service.fixed_blocks_for_date(TUE) == [tuesday_block]
    assert planning_service.fixed_blocks_for_date(MON) == []


def test_delete_fixed_block(planning_service: PlanningService) -> None:
    block = planning_service.save_fixed_block(make_block(MON))
    assert planning_service.delete_fixed_block(block.id, expected_version=block.version) is True
    assert planning_service.delete_fixed_block(block.id, expected_version=block.version) is False


# -----------------------------------------------------------------------------
# Range loading and eligibility
# -----------------------------------------------------------------------------


def test_load_range_returns_a_complete_isolated_snapshot(planning_service: PlanningService) -> None:
    floating = planning_service.save_task(make_task("Floating"))
    next_week = planning_service.save_task(make_task("Next week", required_date=date(2024, 6, 12)))
    planning_service.save_fixed_block(make_block(TUE))
    planning_service.save_fixed_block(make_block(date(2024, 6, 12)))
    planning_service.replace_placements(MON, MON, [make_placement(floating, MON)])
    planning_service.replace_placements(date(2024, 6, 12), date(2024, 6, 12), [make_placement(next_week, date(2024, 6, 12))])

    loaded = planning_service.load_range(MON, WED)

    assert loaded.task_ids == [floating.id]
    assert next_week.id not in loaded.tasks
    assert set(loaded.fixed_blocks_by_date) == {MON, TUE, WED}
    assert [len(loaded.fixed_blocks_by_date[day]) for day in (MON, TUE, WED)] == [0, 1, 0]
    assert [len(loaded.placements_by_date[day]) for day in (MON, TUE, WED)] == [1, 0, 0]

    loaded.tasks.get(floating.id).name = "mutated"
    assert planning_service.get_task(floating.id).name == "Floating"


def test_tasks_for_range_follows_allocation_date_semantics(planning_service: PlanningService) -> None:
    planning_service.save_tasks([
        make_task("floating"),
        make_task("pinned in range", required_date=TUE),
        make_task("pinned elsewhere", required_date=date(2024, 6, 1)),
        make_task("deadline passed", deadline=datetime(2024, 6, 2, 12, tzinfo=timezone.utc)),
        make_task("deadline later", deadline=datetime(2024, 6, 30, tzinfo=timezone.utc)),
    ])

    names = [task.name for task in planning_service.tasks_for_range(MON, WED)]

    assert sorted(names) == ["deadline later", "floating", "pinned in range"]
    assert names == [task.name for task in planning_service.tasks_for_range(MON, WED)]  # deterministic order
