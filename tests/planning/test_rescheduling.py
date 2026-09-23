"""Transactional rescheduling (Milestone 3): occurrence/supersession identity,
cleanup of superseded placements outside the rescheduled range only, and
dependencies outside the range resolved from persisted completion/placement
state (completed / scheduled / skipped / cancelled / pending / missing stay
distinguishable).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import GenerationProvenance, PlanningService, RangeScope
from app.planning.allocation import AllocationReasonCode
from app.planning.errors import VersionConflictError
from app.planning.external_dependencies import (
    ExecutionFact,
    ExternalDependencyState,
    ceil_to_minute,
    resolve_external_dependencies,
)
from app.planning.models import DayScheduleOutput, RecurrenceSpec, ScheduledTask, Task, TaskRegistry, compute_total_score
from app.planning.occurrence import occurrence_key
from app.planning.preferences import OptimizerMode
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus
from app.ui.planning_controller import PlanningController

MON, TUE, WED, THU = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)
LAST_MON = date(2024, 5, 27)


def make_task(name: str, **overrides) -> Task:
    return Task(**{"name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5, **overrides})


def placement(task: Task, day: date, hour: int = 9) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=task.id, planned_date=day, timezone="UTC", planned_start=start,
                         planned_end=start + timedelta(hours=1))


def output(day: date, tasks: list[Task], placements: list[ScheduledTask]) -> DayScheduleOutput:
    return DayScheduleOutput(date=day, timezone="UTC", tasks=TaskRegistry(tasks={t.id: t for t in tasks}),
                             placements=placements, total_score=compute_total_score(placements))


def provenance(start: date, end: date) -> GenerationProvenance:
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return GenerationProvenance(
        allocation_id=uuid.uuid4(), range_start=start, range_end=end, range_scope="planned", fingerprint="f" * 64,
        timezone="UTC", engine_modes={day: OptimizerMode.PRECISE_GREEDY for day in days},
        generated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )


# -----------------------------------------------------------------------------
# Occurrence identity
# -----------------------------------------------------------------------------


def test_occurrence_identity_is_the_task_or_one_date_of_a_recurring_template() -> None:
    single = make_task("Single")
    recurring = make_task("Daily", recurrence=RecurrenceSpec(frequency="daily"))

    assert occurrence_key(placement(single, MON), single) == occurrence_key(placement(single, WED), single)
    assert occurrence_key(placement(recurring, MON), recurring) != occurrence_key(placement(recurring, WED), recurring)
    assert occurrence_key(placement(recurring, MON, 9), recurring) == occurrence_key(placement(recurring, MON, 15), recurring)
    with pytest.raises(ValueError):
        occurrence_key(placement(single, MON), recurring)


# -----------------------------------------------------------------------------
# Supersession cleanup
# -----------------------------------------------------------------------------


def test_rescheduling_removes_only_superseded_placements_outside_the_range(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    moved = make_task("Moved")
    untouched = make_task("Not rescheduled")
    template = make_task("Daily", recurrence=RecurrenceSpec(frequency="daily"))
    started = make_task("Started elsewhere")
    merely_linked = make_task("Linked, not started")
    planning_service.save_tasks([moved, untouched, template, started, merely_linked])

    wednesday = planning_service.replace_placements(WED, WED, [
        placement(moved, WED, 8), placement(untouched, WED, 10), placement(template, WED, 12),
        placement(started, WED, 14), placement(merely_linked, WED, 16),
    ]).placements
    by_task = {p.task_id: p for p in wednesday}
    thursday = planning_service.replace_placements(THU, THU, [placement(untouched, THU)]).placements
    history = execution_service.get_or_create_canonical_execution(started, by_task[started.id])
    execution_service.start(history.id)
    linked = execution_service.get_or_create_canonical_execution(merely_linked, by_task[merely_linked.id])

    new_monday = [placement(task, MON, hour) for task, hour in ((moved, 8), (template, 10), (started, 12), (merely_linked, 14))]
    result = planning_service.reschedule_range(
        MON, MON, {MON: output(MON, [moved, template, started, merely_linked], new_monday)},
        expected_versions={}, provenance=provenance(MON, MON),
    )

    assert set(result.superseded_ids) == {by_task[moved.id].id, by_task[merely_linked.id].id}
    assert result.history_protected_ids == [by_task[started.id].id]
    remaining = {p.task_id for p in planning_service.placements_for_date(WED)}
    assert remaining == {untouched.id, template.id, started.id}  # other tasks, other occurrences, history
    assert planning_service.placements_for_date(THU) == thursday  # an unrelated date is untouched
    assert execution_service.get_execution(linked.id).scheduled_task_id == by_task[merely_linked.id].id  # history kept
    assert len(planning_service.placements_for_date(MON)) == 4


def test_rescheduling_requires_the_previous_placements_it_read(planning_service: PlanningService) -> None:
    task = make_task("Task")
    planning_service.save_task(task)
    [current] = planning_service.replace_placements(MON, MON, [placement(task, MON)]).placements

    with pytest.raises(VersionConflictError):  # generated from an empty day, but the day has a placement now
        planning_service.reschedule_range(
            MON, MON, {MON: output(MON, [task], [placement(task, MON, 15)])},
            expected_versions={}, provenance=provenance(MON, MON),
        )
    assert planning_service.placements_for_date(MON) == [current]
    assert planning_service.generation_record(MON) is None


def test_make_schedule_moves_a_task_it_reschedules_elsewhere(tmp_path: Path) -> None:
    connection = get_connection(tmp_path / "app.db")
    try:
        service = PlanningService(PlanningRepository(connection))
        controller = PlanningController(service=service, timezone="UTC", project_root=str(tmp_path))
        floating = controller.add_or_update_task(make_task("Floating")).value
        week = controller.schedule_range(MON, date(2024, 6, 9), scope=RangeScope.ELIGIBLE).value
        first_day = week.allocation.assignments[floating.id]
        other_day = TUE if first_day != TUE else WED

        rerun = controller.schedule_range(other_day, other_day, scope=RangeScope.ELIGIBLE).value

        assert [p.task_id for p in service.placements_for_date(other_day)] == [floating.id]
        assert service.placements_for_date(first_day) == []  # no double booking
        assert len(rerun.superseded_ids) == 1
        state = controller.day_state(first_day).value
        assert state.status == DayResultStatus.STALE  # its saved schedule changed underneath it
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Dependencies outside the range
# -----------------------------------------------------------------------------


class Stack:
    def __init__(self, tmp_path: Path, completion_clock: datetime) -> None:
        self.connection = get_connection(tmp_path / "app.db")
        self.service = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.service, timezone="UTC", project_root=str(tmp_path))
        self.executions = ExecutionService(ExecutionRepository(self.connection), clock=lambda: completion_clock)

    def scheduled_prerequisite(self) -> tuple[Task, ScheduledTask]:
        prerequisite = self.controller.add_or_update_task(make_task("Prerequisite", required_date=LAST_MON)).value
        assert self.controller.schedule_range(LAST_MON, LAST_MON).ok
        [done] = self.service.placements_for_date(LAST_MON)
        return prerequisite, done

    def dependent_week(self, prerequisite: Task):
        dependent = self.controller.add_or_update_task(
            make_task("Dependent", required_date=MON, dependency_ids=[prerequisite.id])
        ).value
        run = self.controller.schedule_range(MON, date(2024, 6, 9))
        assert run.ok, run.error
        return dependent, run.value

    def close(self) -> None:
        self.connection.close()


@pytest.fixture
def stack(tmp_path: Path):
    session = Stack(tmp_path, datetime(2024, 5, 27, 11, 0, 30, tzinfo=timezone.utc))
    try:
        yield session
    finally:
        session.close()


def test_a_completed_dependency_outside_the_range_is_satisfied(stack: Stack) -> None:
    prerequisite, done = stack.scheduled_prerequisite()
    execution = stack.executions.get_or_create_canonical_execution(prerequisite, done)
    stack.executions.start(execution.id)
    stack.executions.complete(execution.id)

    dependent, run = stack.dependent_week(prerequisite)

    assert run.allocation.assignments[dependent.id] == MON
    assert [p.task_id for p in stack.service.placements_for_date(MON)] == [dependent.id]
    assert stack.service.placements_for_date(LAST_MON) == [done]  # the dependency's own date is untouched


def test_an_earlier_generated_dependency_is_satisfied_by_its_placement(stack: Stack) -> None:
    prerequisite, _ = stack.scheduled_prerequisite()  # generated, not executed

    dependent, run = stack.dependent_week(prerequisite)

    assert run.allocation.assignments[dependent.id] == MON


@pytest.mark.parametrize("action, phrase", [("skip", "was skipped"), ("cancel", "was cancelled")])
def test_skipped_or_cancelled_dependencies_block_with_their_own_reason(stack: Stack, action, phrase) -> None:
    prerequisite, done = stack.scheduled_prerequisite()
    execution = stack.executions.get_or_create_canonical_execution(prerequisite, done)
    getattr(stack.executions, action)(execution.id)

    dependent, run = stack.dependent_week(prerequisite)

    [entry] = [e for e in run.allocation.unallocated if e.task_id == dependent.id]
    assert entry.reason_code == AllocationReasonCode.DEPENDENCY_UNRESOLVED
    assert phrase in entry.explanation
    assert stack.service.placements_for_date(MON) == []


def test_an_allocated_but_never_generated_dependency_is_pending_not_assumed_done(stack: Stack) -> None:
    prerequisite = stack.controller.add_or_update_task(make_task("Prerequisite", required_date=LAST_MON)).value
    allocation = stack.controller.allocate_range(LAST_MON, LAST_MON).value
    assert allocation.assignments[prerequisite.id] == LAST_MON  # assigned a date, but never generated

    dependent, run = stack.dependent_week(prerequisite)

    [entry] = [e for e in run.allocation.unallocated if e.task_id == dependent.id]
    assert "neither completed nor scheduled" in entry.explanation


def test_a_missing_dependency_is_reported_as_missing(stack: Stack) -> None:
    prerequisite = stack.controller.add_or_update_task(make_task("Prerequisite", required_date=LAST_MON)).value
    dependent = stack.controller.add_or_update_task(
        make_task("Dependent", required_date=MON, dependency_ids=[prerequisite.id])
    ).value
    # Historical data: a dependency deleted before deletions were guarded (the service refuses this today).
    PlanningRepository(stack.connection).soft_delete_task(
        prerequisite.id, deleted_at=datetime(2024, 5, 1, tzinfo=timezone.utc), expected_version=1
    )

    run = stack.controller.schedule_range(MON, date(2024, 6, 9)).value

    [entry] = [e for e in run.allocation.unallocated if e.task_id == dependent.id]
    assert "does not exist" in entry.explanation


def test_same_day_completion_is_honoured_to_the_next_whole_minute(tmp_path: Path) -> None:
    stack = Stack(tmp_path, datetime(2024, 6, 3, 10, 0, 30, tzinfo=timezone.utc))  # completed on MON itself
    try:
        prerequisite, done = stack.scheduled_prerequisite()
        execution = stack.executions.get_or_create_canonical_execution(prerequisite, done)
        stack.executions.start(execution.id)
        stack.executions.complete(execution.id)

        dependent, _ = stack.dependent_week(prerequisite)

        [scheduled] = stack.service.placements_for_date(MON)
        assert scheduled.task_id == dependent.id
        assert scheduled.planned_start >= datetime(2024, 6, 3, 10, 1, tzinfo=timezone.utc)
    finally:
        stack.close()


def test_resolution_states_are_distinguishable() -> None:
    ids = {name: uuid.uuid4() for name in ("missing", "done", "planned", "later", "skipped", "cancelled", "pending")}
    done_at = datetime(2024, 6, 1, 9, 30, 15, tzinfo=timezone.utc)

    def fact(task_id, status, finished=None, updated="2024-06-01T00:00:00+00:00", placement_id=None):
        return ExecutionFact(task_id=task_id, scheduled_task_id=placement_id, status=status, finished_at=finished,
                             updated_at=updated)

    planned = placement(make_task("planned", id=ids["planned"]), date(2024, 5, 30))
    later = placement(make_task("later", id=ids["later"]), date(2024, 6, 20))
    resolved = resolve_external_dependencies(
        ids.values(), range_start=MON, range_end=date(2024, 6, 9), timezone_name="UTC",
        persisted_task_ids=set(ids.values()) - {ids["missing"]},
        placements_by_task={ids["planned"]: [planned], ids["later"]: [later]},
        executions_by_task={
            ids["done"]: [fact(ids["done"], "skipped"), fact(ids["done"], "completed", done_at)],
            ids["skipped"]: [fact(ids["skipped"], "cancelled", updated="1"), fact(ids["skipped"], "skipped", updated="2")],
            ids["cancelled"]: [fact(ids["cancelled"], "cancelled")],
            ids["pending"]: [fact(ids["pending"], "in_progress")],
        },
    )

    states = {name: resolved[task_id].state for name, task_id in ids.items()}
    assert states == {
        "missing": ExternalDependencyState.MISSING,
        "done": ExternalDependencyState.COMPLETED,
        "planned": ExternalDependencyState.SCHEDULED,
        "later": ExternalDependencyState.SCHEDULED_LATER,
        "skipped": ExternalDependencyState.SKIPPED,
        "cancelled": ExternalDependencyState.CANCELLED,
        "pending": ExternalDependencyState.PENDING,
    }
    assert resolved[ids["done"]].satisfied_at == done_at and resolved[ids["done"]].satisfied_date == date(2024, 6, 1)
    assert resolved[ids["planned"]].satisfied_at == planned.planned_end
    assert [name for name, task_id in ids.items() if resolved[task_id].satisfied] == ["done", "planned"]
    assert ceil_to_minute(done_at) == datetime(2024, 6, 1, 9, 31, tzinfo=timezone.utc)
