"""Manual placements and generation scope (docs/execution-rescheduling.md, "Manual
placements" and "Scope"): a move's destination is the user's manual intent --
every generation keeps it until it is released, across restarts; releasing
changes nothing but the intent; an older placement is preserved only when its
recorded lineage proves it is a move's destination; work live outside the
generated dates is left there and reported; kept work that no longer fits is a
structured, zero-write conflict (history only a notice); a change to manual
intent or execution state during a generation refuses the save; a replaced
never-started attempt is cancelled as superseded, never skipped."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.models import CancelReason, ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.errors import InvalidEntityError, RegenerationRequiredError, StaleInputsError, VersionConflictError
from app.planning.models import FixedBlock, PlacementOrigin, RecurrenceSpec, ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.ui.planning_controller import PlanningController

MON, TUE = date(2024, 6, 3), date(2024, 6, 4)


def at(hour: int, day: date = MON, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


class Stack:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.open()

    def open(self) -> None:
        self.connection = get_connection(self.db_path)
        self.planning = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.planning, timezone="UTC", project_root=str(self.db_path.parent))
        self.executions = ExecutionService(ExecutionRepository(self.connection))

    def restart(self) -> None:
        self.connection.close()
        self.open()

    def task(self, name: str = "Study", **fields) -> Task:
        return self.controller.add_or_update_task(Task(**{
            "name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5,
            "required_date": MON, **fields})).value

    def generate(self, start: date = MON, end: date | None = None, **kwargs) -> workflow.GenerationOutcome:
        return workflow.generate(self.planning, range_start=start, range_end=end or start, timezone_name="UTC",
                                 scope=RangeScope.ELIGIBLE, **kwargs)

    def placements(self, task: Task) -> list[ScheduledTask]:
        return [p for group in self.planning.active_placements_for_tasks([task.id]).values() for p in group]

    def only(self, task: Task) -> ScheduledTask:
        [placement] = self.placements(task)
        return placement

    def move(self, placement: ScheduledTask, hour: int, day: date = MON, minutes: int = 60) -> ScheduledTask:
        return workflow.reschedule_placement(
            self.planning, placement.id, expected_version=placement.version, planned_date=day, timezone_name="UTC",
            planned_start=at(hour, day), planned_end=at(hour, day) + timedelta(minutes=minutes)).replacement

    def block(self, start: datetime, end: datetime, day: date = MON) -> FixedBlock:
        return self.planning.create_fixed_block(FixedBlock(label="Class", category="event", planned_date=day,
                                                           timezone="UTC", planned_start=start, planned_end=end))

    def state(self) -> tuple:
        return tuple([tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                     for table in ("scheduled_tasks", "executions", "schedule_generations", "sync_dirty"))


@pytest.fixture
def stack(tmp_path: Path):
    session = Stack(tmp_path / "app.db")
    yield session
    session.connection.close()


def moved_task(stack: Stack, **fields) -> tuple[Task, ScheduledTask]:
    """A task scheduled by generation and then moved by the user to 15:00."""
    task = stack.task(**fields)
    stack.generate()
    return task, stack.move(stack.only(task), 15)


# -----------------------------------------------------------------------------
# Manual intent survives generation, restarts, and is released explicitly
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [workflow.GenerationMode.FULL, workflow.GenerationMode.INCREMENTAL])
def test_a_moved_placement_survives_every_generation_and_a_restart(stack: Stack, mode) -> None:
    task, moved = moved_task(stack)
    assert (moved.origin, moved.preserved) == (PlacementOrigin.MANUAL, True)
    other = stack.task("Other")  # the date is out of date: generation has new work to place

    outcome = stack.generate(mode=mode)
    assert outcome.status == "generated" and moved.id in outcome.kept_ids[MON]
    assert stack.only(task) == moved
    new = stack.only(other)
    assert new.origin == PlacementOrigin.GENERATED and not new.preserved
    assert new.planned_end <= moved.planned_start or new.planned_start >= moved.planned_end  # placed around it

    stack.restart()
    stack.controller.add_or_update_task(other.model_copy(update={"priority": 9}), expected_version=other.version)
    stack.generate()
    assert stack.only(task) == moved  # the intent is stored, not held in memory


def test_an_unchanged_generation_after_a_move_writes_nothing(stack: Stack) -> None:
    _, moved = moved_task(stack)
    assert stack.generate().status == "generated"  # the move made the date stale
    before = stack.state()

    again = stack.generate()

    assert again.status == "already_current" and stack.state() == before


def test_releasing_keeps_the_placement_and_hands_it_back_to_generation(stack: Stack) -> None:
    task, moved = moved_task(stack)
    with pytest.raises(VersionConflictError):
        stack.planning.release_manual_placement(moved.id, expected_version=moved.version + 1)
    released = stack.planning.release_manual_placement(moved.id, expected_version=moved.version)
    assert released.model_dump(exclude={"preserved", "version", "updated_at"}) == moved.model_dump(
        exclude={"preserved", "version", "updated_at"})
    with pytest.raises(InvalidEntityError):  # nothing left to release
        stack.planning.release_manual_placement(moved.id, expected_version=released.version)

    stack.generate(mode=workflow.GenerationMode.FULL)

    replacement = stack.only(task)
    assert replacement.id != moved.id and replacement.origin == PlacementOrigin.GENERATED
    tombstone = stack.planning.get_placement(moved.id, include_deleted=True)
    assert tombstone.superseded_by_id == replacement.id and tombstone.preserved is False


def test_an_older_placement_is_preserved_only_when_its_lineage_proves_a_move(stack: Stack) -> None:
    task, moved = moved_task(stack)
    plain = stack.task("Plain")
    stack.generate()
    generated = stack.only(plain)
    # As saved before origins were recorded: unknown origin, no stored intent.
    stack.connection.execute("UPDATE scheduled_tasks SET origin = NULL, preserved = 0")

    stored = stack.planning.placements_for_date(MON)
    assert stack.planning.preserved_placement_ids(stored) == {moved.id}  # proven by its RESCHEDULED predecessor

    stack.controller.add_or_update_task(plain.model_copy(update={"estimated_duration_minutes": 45}),
                                        expected_version=plain.version)
    stack.generate()
    assert stack.only(task).id == moved.id  # kept as manual intent
    assert stack.only(plain).id != generated.id  # unknown origin without that proof is replaceable
    released = stack.planning.release_manual_placement(moved.id, expected_version=stack.only(task).version)
    assert (released.origin, released.preserved) == (PlacementOrigin.MANUAL, False)


# -----------------------------------------------------------------------------
# Scope: dates outside the generated ones are never touched
# -----------------------------------------------------------------------------


def test_work_live_on_another_date_is_left_there_and_reported(stack: Stack) -> None:
    task = stack.task(required_date=None, preferred_dates=[MON])
    stack.generate()
    moved = stack.move(stack.only(task), 10, day=TUE)
    tuesday = stack.planning.placements_for_date(TUE)

    outcome = stack.generate(MON)

    assert [p for p in stack.planning.placements_for_date(MON) if p.task_id == task.id] == []
    assert stack.planning.placements_for_date(TUE) == tuesday  # untouched: same ids and versions
    assert outcome.kept_elsewhere[task.id].id == moved.id
    assert outcome.reschedule.superseded_ids == []


def test_a_recurring_occurrence_moved_to_another_date_is_not_placed_again(stack: Stack) -> None:
    daily = stack.task("Daily", required_date=None,
                       recurrence=RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC"))
    stack.generate(MON, TUE)
    occurrences = {t.occurrence_slot: t for t in stack.planning.list_tasks() if t.series_id == daily.id}
    monday = occurrences[MON]
    moved = stack.move(stack.only(monday), 16, day=TUE)

    stack.generate(MON, TUE)  # both dates, FULL

    assert stack.placements(monday) == [moved]  # kept on Tuesday as the user put it, not placed on Monday
    assert len(stack.placements(occurrences[TUE])) == 1  # Tuesday's own occurrence is planned as usual


# -----------------------------------------------------------------------------
# Conflicts: structured, zero-write; history is only a notice
# -----------------------------------------------------------------------------


def test_a_manual_placement_that_no_longer_fits_blocks_with_remedies_and_writes_nothing(stack: Stack) -> None:
    task, moved = moved_task(stack)
    stack.block(at(15), at(16))
    before = stack.state()

    with pytest.raises(RegenerationRequiredError) as refused:
        stack.generate(mode=workflow.GenerationMode.FULL)

    [problem] = refused.value.problems
    assert (problem.placement_id, problem.reason, problem.kept_as, problem.blocking) == (
        moved.id, "overlaps_fixed_block", "manual", True)
    assert {"move", "release_manual_intent", "edit_constraint"} <= set(problem.remedies)
    assert stack.state() == before


def test_a_conflict_on_one_date_rolls_back_every_date(stack: Stack) -> None:
    monday = stack.task("Monday")
    tuesday = stack.task("Tuesday", required_date=TUE)
    stack.generate(MON, TUE)
    stack.move(stack.only(tuesday), 15, day=TUE)
    stack.block(at(15, TUE), at(16, TUE), day=TUE)
    stack.controller.add_or_update_task(monday.model_copy(update={"estimated_duration_minutes": 30}),
                                        expected_version=monday.version)
    before = stack.state()

    with pytest.raises(RegenerationRequiredError):
        stack.generate(MON, TUE)

    assert stack.state() == before  # Monday, which alone would have generated, is not written either


def test_history_that_no_longer_fits_is_kept_as_a_notice_and_new_work_avoids_it(stack: Stack) -> None:
    task = stack.task()
    stack.generate()
    worked = stack.only(task)
    execution = stack.executions.get_or_create_canonical_execution(task, worked)
    stack.executions.start(execution.id)
    stack.block(worked.planned_start, worked.planned_start + timedelta(minutes=30))  # added after work started
    stack.controller.add_or_update_task(task.model_copy(update={"estimated_duration_minutes": 90}),
                                        expected_version=task.version)  # an estimate mismatch with history
    other = stack.task("Other", estimated_duration_minutes=600)

    outcome = stack.generate()

    assert stack.only(task) == worked  # history stays exactly as recorded
    reasons = {(p.placement_id, p.reason, p.blocking, p.kept_as) for p in outcome.notices[MON]}
    assert reasons == {(worked.id, "overlaps_fixed_block", False, "history")}  # the estimate is not judged
    for placement in stack.placements(other):
        assert placement.planned_end <= worked.planned_start or placement.planned_start >= worked.planned_end


# -----------------------------------------------------------------------------
# Concurrency and execution dispositions
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["release", "start", "move"])
def test_a_change_during_generation_refuses_the_save(stack: Stack, monkeypatch, change) -> None:
    task, moved = moved_task(stack)
    plain = stack.task("Plain", required_date=MON)
    stack.generate()
    planned = stack.only(plain)
    execution = stack.executions.get_or_create_canonical_execution(plain, planned)
    stack.controller.add_or_update_task(plain.model_copy(update={"priority": 8}), expected_version=plain.version)
    real = workflow.generate_selected_day
    raced = []

    def racing(*args, **kwargs):  # another writer commits while the engine runs (outside any transaction)
        if not raced:
            if change == "release":
                stack.planning.release_manual_placement(moved.id, expected_version=moved.version)
            elif change == "start":
                stack.executions.start(execution.id)
            else:
                stack.move(stack.only(task), 17)
            raced.append(stack.state())
        return real(*args, **kwargs)

    monkeypatch.setattr(workflow, "generate_selected_day", racing)
    with pytest.raises(StaleInputsError) as refused:
        stack.generate()

    assert "manual placements or execution states" in str(refused.value) or change == "move"
    assert stack.state() == raced[0]  # only the racing change is stored; the generation wrote nothing
    assert stack.planning.get_placement(planned.id) is not None


def test_a_replaced_never_started_attempt_is_cancelled_as_superseded(stack: Stack) -> None:
    task = stack.task()
    stack.generate()
    placement = stack.only(task)
    execution = stack.executions.get_or_create_canonical_execution(task, placement)
    stack.controller.add_or_update_task(task.model_copy(update={"estimated_duration_minutes": 90}),
                                        expected_version=task.version)

    stack.generate()

    cancelled = stack.executions.get_execution(execution.id)
    assert (cancelled.status, cancelled.cancel_reason) == (ExecutionStatus.CANCELLED, CancelReason.SUPERSEDED)
    assert cancelled.scheduled_task_id == placement.id  # its snapshot still names the plan it belonged to
    assert stack.only(task).id != placement.id


def test_a_moved_attempt_is_cancelled_as_rescheduled_and_a_user_cancel_is_the_users(stack: Stack) -> None:
    task = stack.task()
    stack.generate()
    placement = stack.only(task)
    first = stack.executions.get_or_create_canonical_execution(task, placement)
    moved = stack.move(placement, 15)
    assert stack.executions.get_execution(first.id).cancel_reason == CancelReason.RESCHEDULED

    second = stack.executions.get_or_create_canonical_execution(task, moved)
    assert stack.executions.cancel(second.id).cancel_reason == CancelReason.USER
