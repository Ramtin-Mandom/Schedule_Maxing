"""Explicit rescheduling and the regeneration audit on the local SQLite store
(docs/execution-rescheduling.md): a move keeps the original plan and names
its replacement; a regeneration leaves unchanged placements alone and gives
every removed one its reason and successor; chains of moves stay traceable;
started or finished history is never replaced through any generation entry
point; recurrence templates keep one occurrence per date; a failed move
changes nothing (change capture included); owner scopes hold."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.errors import (
    DuplicateEntityError,
    EntityNotFoundError,
    RescheduleRejectedError,
    VersionConflictError,
)
from app.planning.models import FixedBlock, PlacementRemovalReason, RecurrenceSpec, ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.ui.planning_controller import PlanningController

MON, TUE = date(2024, 6, 3), date(2024, 6, 4)


def at(hour: int, day: date = MON, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.connection = get_connection(tmp_path / "app.db")
        self.planning = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.planning, timezone="UTC", project_root=str(tmp_path))
        self.executions = ExecutionService(ExecutionRepository(self.connection))

    def task(self, name: str = "Study", **fields) -> Task:
        return self.controller.add_or_update_task(Task(**{
            "name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5,
            "required_date": MON, **fields})).value

    def schedule(self, day: date = MON):
        result = self.controller.schedule_range(day, day, scope=RangeScope.ELIGIBLE)
        assert result.ok, result.error
        return result.value

    def only(self, task: Task, day: date = MON) -> ScheduledTask:
        [placement] = [p for p in self.planning.placements_for_date(day) if p.task_id == task.id]
        return placement

    def move(self, placement: ScheduledTask, hour: int, day: date = MON, minutes: int = 60, **kwargs):
        return workflow.reschedule_placement(
            self.planning, placement.id, expected_version=placement.version, planned_date=day, timezone_name="UTC",
            planned_start=at(hour, day), planned_end=at(hour, day) + timedelta(minutes=minutes), **kwargs)

    def dirty(self) -> list[tuple]:
        return [tuple(row) for row in self.connection.execute(
            "SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY entity_type, entity_id")]

    def rows(self, table: str) -> list[tuple]:
        return [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]


@pytest.fixture
def stack(tmp_path: Path):
    session = Stack(tmp_path)
    yield session
    session.connection.close()


# -----------------------------------------------------------------------------
# Regeneration
# -----------------------------------------------------------------------------


def test_an_unchanged_regeneration_keeps_every_placement_and_records_no_move(stack: Stack) -> None:
    task = stack.task()
    stack.schedule()
    before = stack.only(task)
    dirty = stack.dirty()

    rerun = stack.schedule()  # the same inputs again, through the Make Schedule path

    after = stack.only(task)
    assert after == before  # same id, same version: not rewritten
    assert rerun.superseded_ids == [] and rerun.replacement.removed_ids == []
    assert [row for row in stack.dirty() if row[0] == "placement"] == [r for r in dirty if r[0] == "placement"]
    assert stack.planning.placements_superseded_by([before.id]) == {}
    assert before.task_category == "study"


def test_a_changed_placement_keeps_its_original_plan_and_names_its_successor(stack: Stack) -> None:
    task = stack.task()
    stack.schedule()
    original = stack.only(task)
    stack.controller.add_or_update_task(task.model_copy(update={"estimated_duration_minutes": 90, "category": "deep"}),
                                        expected_version=task.version)

    stack.schedule()

    replacement = stack.only(task)
    tombstone = stack.planning.get_placement(original.id, include_deleted=True)
    assert replacement.id != original.id and replacement.task_category == "deep"
    assert tombstone.deleted_at is not None and tombstone.removal_reason == PlacementRemovalReason.REGENERATED
    assert tombstone.superseded_by_id == replacement.id
    assert (tombstone.planned_start, tombstone.planned_end, tombstone.task_category) == (
        original.planned_start, original.planned_end, "study")  # the plan as it was, category included


def test_a_move_then_a_regeneration_is_one_traceable_chain(stack: Stack) -> None:
    task = stack.task()
    stack.schedule()
    first = stack.only(task)
    moved = stack.move(first, 15).replacement

    stack.schedule()  # a full regeneration replaces the manually moved placement like any other

    last = stack.only(task)
    assert last.id not in (first.id, moved.id)
    chain = [last.id]
    while predecessors := stack.planning.placements_superseded_by([chain[-1]]).get(chain[-1]):
        chain.append(predecessors[0].id)
    assert chain == [last.id, moved.id, first.id]  # newest first: one occurrence, deduplicated by walking back
    reasons = [stack.planning.get_placement(pid, include_deleted=True).removal_reason for pid in chain[1:]]
    assert reasons == [PlacementRemovalReason.REGENERATED, PlacementRemovalReason.RESCHEDULED]
    assert stack.planning.get_placement(first.id, include_deleted=True).planned_start == first.planned_start


@pytest.mark.parametrize("finish", [False, True], ids=["started", "completed"])
@pytest.mark.parametrize("entry", ["schedule_range", "generate_day", "generate"])
def test_started_or_finished_history_survives_every_generation_entry_point(stack: Stack, entry, finish) -> None:
    task = stack.task()
    stack.schedule()
    worked = stack.only(task)
    execution = stack.executions.get_or_create_canonical_execution(task, worked)
    stack.executions.start(execution.id)
    if finish:
        stack.executions.complete(execution.id)
    history = (stack.executions.get_execution(execution.id), stack.executions.list_sessions(execution.id))
    # Inputs change so that the engine alone would place the task differently.
    stack.controller.add_or_update_task(task.model_copy(update={"estimated_duration_minutes": 90}),
                                        expected_version=task.version)

    if entry == "schedule_range":
        stack.schedule()
    elif entry == "generate_day":
        assert stack.controller.allocate_range(MON, MON).ok
        assert stack.controller.generate_day(MON).ok
    else:
        assert stack.controller.generate(MON, MON, scope=RangeScope.ELIGIBLE).ok

    assert stack.planning.placements_for_date(MON) == [worked]  # kept exactly; the task is not placed again
    assert (stack.executions.get_execution(execution.id), stack.executions.list_sessions(execution.id)) == history


# -----------------------------------------------------------------------------
# Explicit moves: rules and atomicity
# -----------------------------------------------------------------------------


def test_a_recurring_template_placement_moves_only_within_its_own_date(stack: Stack) -> None:
    daily = stack.task("Daily", required_date=None, recurrence=RecurrenceSpec(frequency="daily"))
    stack.schedule()
    placement = stack.only(daily)

    with pytest.raises(RescheduleRejectedError) as rejected:
        stack.move(placement, 10, day=TUE)  # another date is another occurrence
    assert [p.reason for p in rejected.value.problems] == ["recurring_occurrence_date"]

    moved = stack.move(placement, 16)
    assert moved.replacement.planned_date == MON and moved.previous.superseded_by_id == moved.replacement.id


def test_hard_rules_of_the_destination(stack: Stack) -> None:
    task = stack.task(deadline=at(17))
    stack.schedule()
    placement = stack.only(task)
    stack.planning.create_fixed_block(FixedBlock(label="Class", category="event", planned_date=MON, timezone="UTC",
                                                 planned_start=at(12), planned_end=at(14)))

    def reasons(**kwargs) -> list[str]:
        with pytest.raises(RescheduleRejectedError) as rejected:
            stack.move(placement, **kwargs)
        return [problem.reason for problem in rejected.value.problems]

    assert reasons(hour=13) == ["overlaps_fixed_block"]
    assert reasons(hour=10, day=TUE) == ["required_date", "deadline_missed"]  # every problem is reported
    assert reasons(hour=16, minutes=90) == ["deadline_missed"]
    assert reasons(hour=10, minutes=45) == ["duration_changed"]
    assert stack.planning.placements_for_date(MON) == [placement]


def test_a_failure_part_way_through_a_move_changes_nothing(stack: Stack, monkeypatch) -> None:
    task = stack.task()
    stack.schedule()
    placement = stack.only(task)
    execution = stack.executions.get_or_create_canonical_execution(task, placement)
    snapshot = (stack.rows("scheduled_tasks"), stack.rows("executions"), stack.dirty())

    def fail(*_args, **_kwargs):
        raise RuntimeError("disk full (injected)")

    monkeypatch.setattr(PlanningRepository, "cancel_unstarted_execution", fail)
    with pytest.raises(RuntimeError):
        stack.move(placement, 15)  # the tombstone and the replacement were already written in the transaction

    assert (stack.rows("scheduled_tasks"), stack.rows("executions"), stack.dirty()) == snapshot
    assert stack.executions.get_execution(execution.id).status.value == "scheduled"


def test_owner_scopes_and_identities_are_respected(stack: Stack) -> None:
    task = stack.task()
    stack.schedule()
    placement = stack.only(task)
    elsewhere = stack.planning.scoped(OwnerScope.account(uuid.uuid4()))
    with pytest.raises(EntityNotFoundError):  # another owner's placement is invisible
        workflow.reschedule_placement(elsewhere, placement.id, expected_version=placement.version, planned_date=MON,
                                      timezone_name="UTC", planned_start=at(15), planned_end=at(16))

    first = stack.move(placement, 15)
    with pytest.raises(VersionConflictError) as moved_already:
        stack.move(placement, 16)
    assert moved_already.value.deleted
    with pytest.raises(DuplicateEntityError):  # a replacement id is never reused, not even a tombstone's
        stack.move(first.replacement, 17, replacement_id=placement.id)
    assert [p.id for p in stack.planning.placements_for_date(MON)] == [first.replacement.id]


def test_resets_and_task_deletions_record_why_placements_left(stack: Stack) -> None:
    kept, removed = stack.task("Kept"), stack.task("Removed")
    stack.schedule()
    kept_placement, removed_placement = stack.only(kept), stack.only(removed)

    stack.planning.delete_task(removed.id, expected_version=stack.planning.get_task(removed.id).version)
    stack.planning.clear_range(MON, MON, include_planning_data=False)

    def reason(placement: ScheduledTask) -> PlacementRemovalReason | None:
        return stack.planning.get_placement(placement.id, include_deleted=True).removal_reason

    assert reason(removed_placement) == PlacementRemovalReason.TASK_DELETED
    assert reason(kept_placement) == PlacementRemovalReason.RESET
