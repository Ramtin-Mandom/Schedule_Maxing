"""The Uncompleted | Tasks | Completed outcomes on the execution model (app/execution/lifecycle.py
and ExecutionService, SQLite): one canonical state per column (completed / skipped / the pending
statuses), the actions that move between them, "complete" without timing, "reopen" keeping every
session and the first start while withdrawing only the finishing marker and its metrics, and
set_outcome -- one transaction, one execution per placement, keyed by placement id, conflicts
reported rather than overwritten."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.errors import ExecutionVersionConflictError, InvalidTransitionError
from app.execution.lifecycle import (
    TRANSITIONS,
    OutcomeChangeError,
    TaskOutcome,
    outcome_actions,
    outcome_of,
    reopen_target,
)
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.models import RecurrenceFrequency, RecurrenceSpec, ScheduledTask, Task
from app.planning.repository import PlanningRepository

S = ExecutionStatus
T0 = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(repository: ExecutionRepository, clock: Clock) -> ExecutionService:
    return ExecutionService(repository, clock=clock)


@pytest.fixture
def plan(planning_repository: PlanningRepository):
    """A persisted task and one placement of it (start hour on 2024-06-03 UTC)."""

    def make(name: str = "Study", hour: int = 9, day_offset: int = 0, task: Task | None = None, **task_fields):
        if task is None:
            task = Task(name=name, category="study", estimated_duration_minutes=60, priority=5, **task_fields)
            planning_repository.insert_task(task)
        start = T0.replace(hour=hour) + timedelta(days=day_offset)
        placement = ScheduledTask(task_id=task.id, planned_date=start.date(), timezone="UTC", planned_start=start,
                                  planned_end=start + timedelta(hours=1), score=1.0)
        planning_repository.insert_placement(placement)
        return task, placement

    return make


# -----------------------------------------------------------------------------
# The pure rules
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("status, outcome", [
    (None, TaskOutcome.PENDING), (S.SCHEDULED, TaskOutcome.PENDING), (S.IN_PROGRESS, TaskOutcome.PENDING),
    (S.PAUSED, TaskOutcome.PENDING), (S.COMPLETED, TaskOutcome.COMPLETED), (S.SKIPPED, TaskOutcome.UNCOMPLETED),
    (S.CANCELLED, TaskOutcome.UNCOMPLETED),
])
def test_every_status_has_exactly_one_column(status, outcome) -> None:
    assert outcome_of(status) == outcome


@pytest.mark.parametrize("status, target, actions", [
    (None, TaskOutcome.COMPLETED, ("complete",)),
    (None, TaskOutcome.UNCOMPLETED, ("skip",)),
    (None, TaskOutcome.PENDING, ()),
    (S.SCHEDULED, TaskOutcome.COMPLETED, ("complete",)),
    (S.PAUSED, TaskOutcome.COMPLETED, ("complete",)),
    (S.IN_PROGRESS, TaskOutcome.UNCOMPLETED, ("skip",)),
    (S.COMPLETED, TaskOutcome.PENDING, ("reopen",)),
    (S.SKIPPED, TaskOutcome.PENDING, ("reopen",)),
    (S.SKIPPED, TaskOutcome.COMPLETED, ("reopen", "complete")),
    (S.COMPLETED, TaskOutcome.UNCOMPLETED, ("reopen", "skip")),
    (S.COMPLETED, TaskOutcome.COMPLETED, ()),
])
def test_moves_are_lifecycle_actions(status, target, actions) -> None:
    assert outcome_actions(status, target) == actions
    for action in actions:  # every step is a legal transition of the one table
        assert action in TRANSITIONS


def test_a_cancelled_attempt_is_not_reopened() -> None:
    with pytest.raises(OutcomeChangeError):
        outcome_actions(S.CANCELLED, TaskOutcome.PENDING)
    assert S.CANCELLED not in TRANSITIONS["reopen"][0]
    assert (reopen_target(False), reopen_target(True)) == (S.SCHEDULED, S.PAUSED)


# -----------------------------------------------------------------------------
# reopen keeps history
# -----------------------------------------------------------------------------


def test_reopening_a_timed_completion_keeps_sessions_and_first_start(service, plan, clock) -> None:
    task, placement = plan()
    execution = service.get_or_create_canonical_execution(task, placement)
    started = service.start(execution.id)
    clock.now = T0 + timedelta(minutes=40)
    done = service.complete(execution.id)
    assert done.actual_active_duration_minutes == 40.0 and done.actual_final_end_at == clock.now

    clock.now = T0 + timedelta(hours=2)
    reopened = service.reopen(execution.id, expected_version=done.version)
    assert reopened.status == S.PAUSED  # work was recorded: pending, not "never started"
    assert reopened.version == done.version + 1
    assert reopened.actual_first_start_at == started.actual_first_start_at  # actual history untouched
    assert len(service.list_sessions(execution.id)) == 1
    assert (reopened.actual_final_end_at, reopened.actual_active_duration_minutes,
            reopened.duration_variance_minutes, reopened.start_delay_minutes) == (None, None, None, None)
    assert reopened.canonical_planned_start == done.canonical_planned_start  # the plan snapshot never changes

    again = service.complete(execution.id)  # finishing again recomputes from the kept sessions
    assert again.actual_active_duration_minutes == 40.0 and again.actual_final_end_at == clock.now


def test_reopening_an_untimed_outcome_returns_to_scheduled(service, plan) -> None:
    task, placement = plan()
    execution = service.get_or_create_canonical_execution(task, placement)
    skipped = service.skip(execution.id)
    back = service.reopen(execution.id)
    assert back.status == S.SCHEDULED and back.actual_final_end_at is None and skipped.version + 1 == back.version
    with pytest.raises(InvalidTransitionError):
        service.reopen(execution.id)  # already pending


def test_cancelled_and_pending_attempts_cannot_be_reopened(service, plan) -> None:
    task, placement = plan()
    execution = service.get_or_create_canonical_execution(task, placement)
    with pytest.raises(InvalidTransitionError):
        service.reopen(execution.id)
    service.cancel(execution.id)
    with pytest.raises(InvalidTransitionError):
        service.reopen(execution.id)


# -----------------------------------------------------------------------------
# set_outcome
# -----------------------------------------------------------------------------


def test_set_outcome_walks_every_column_with_one_execution(service, plan) -> None:
    task, placement = plan()
    assert service.set_outcome(task, placement, TaskOutcome.PENDING) is None  # nothing to record, nothing created
    assert service.find_execution_for_placement(placement.id) is None

    done = service.set_outcome(task, placement, "completed")
    assert done.status == S.COMPLETED and done.scheduled_task_id == placement.id and done.task_id == task.id
    back = service.set_outcome(task, placement, TaskOutcome.PENDING, expected_version=done.version)
    assert back.status == S.SCHEDULED and back.id == done.id
    missed = service.set_outcome(task, placement, TaskOutcome.UNCOMPLETED, expected_version=back.version)
    assert missed.status == S.SKIPPED
    done_after_all = service.set_outcome(task, placement, TaskOutcome.COMPLETED, expected_version=missed.version)
    assert done_after_all.status == S.COMPLETED
    assert [execution.id for execution in service.list_executions()] == [done.id]  # never a duplicate


def test_set_outcome_refuses_a_stale_view(service, plan) -> None:
    task, placement = plan()
    done = service.set_outcome(task, placement, TaskOutcome.COMPLETED)
    with pytest.raises(ExecutionVersionConflictError):
        service.set_outcome(task, placement, TaskOutcome.PENDING, expected_version=done.version - 1,
                            require_version=True)
    with pytest.raises(ExecutionVersionConflictError):  # showed "no execution", but one exists now
        service.set_outcome(task, placement, TaskOutcome.UNCOMPLETED, expected_version=None, require_version=True)
    assert service.get_execution(done.id).status == S.COMPLETED  # nothing changed
    other_task, other = plan("Other", hour=11)
    with pytest.raises(ExecutionVersionConflictError):  # showed an execution that does not exist
        service.set_outcome(other_task, other, TaskOutcome.COMPLETED, expected_version=3, require_version=True)
    assert service.find_execution_for_placement(other.id) is None
    # Without a precondition the outcome is applied to whatever is stored (bulk actions use this).
    assert service.set_outcome(task, placement, TaskOutcome.UNCOMPLETED).status == S.SKIPPED


def test_duplicate_names_and_recurring_occurrences_never_share_a_status(service, plan) -> None:
    first_task, first = plan("Read", hour=9)
    second_task, second = plan("Read", hour=13)  # another task with the same name
    service.set_outcome(first_task, first, TaskOutcome.COMPLETED)
    found = service.executions_for_placements([first.id, second.id])
    assert set(found) == {first.id} and found[first.id].status == S.COMPLETED

    daily, monday = plan("Walk", hour=18, recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.DAILY))
    _, tuesday = plan(task=daily, hour=18, day_offset=1)
    service.set_outcome(daily, monday, TaskOutcome.UNCOMPLETED)
    service.set_outcome(daily, tuesday, TaskOutcome.COMPLETED)
    found = service.executions_for_placements([monday.id, tuesday.id])
    assert (found[monday.id].status, found[tuesday.id].status) == (S.SKIPPED, S.COMPLETED)
