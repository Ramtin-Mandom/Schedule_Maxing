"""Execution and planning integration from saved placements to persisted outcomes and cohort reports."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.productivity.reporting import ProductivityService
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController

TZ = "America/Vancouver"
MON = date(2026, 3, 2)


def at(hour: int, minute: int = 0) -> datetime:  # local Vancouver time on MON (UTC-8)
    return datetime(2026, 3, 2, tzinfo=timezone.utc) + timedelta(hours=hour + 8, minutes=minute)


class Clock:
    def __init__(self) -> None:
        self.now = at(7)

    def __call__(self) -> datetime:
        return self.now


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.connection = get_connection(tmp_path / "app.db")
        self.planning_service = PlanningService(PlanningRepository(self.connection), self.clock)
        self.execution_service = ExecutionService(ExecutionRepository(self.connection), self.clock)
        self.planning = PlanningController(service=self.planning_service, timezone=TZ, project_root=str(tmp_path))
        self.executions = ExecutionController(self.execution_service)
        self.productivity = ProductivityService(
            ExecutionRepository(self.connection), clock=self.clock, history=self.planning_service, timezone_name=TZ,
        )

    def planned(self, name: str = "Study", hour: int = 9, minutes: int = 60):
        task = self.planning_service.create_task(Task(name=name, category="study", estimated_duration_minutes=minutes))
        placement = ScheduledTask(task_id=task.id, planned_date=MON, timezone=TZ, planned_start=at(hour),
                                  planned_end=at(hour) + timedelta(minutes=minutes), created_at=self.clock.now,
                                  updated_at=self.clock.now)
        existing = self.planning_service.placements_for_date(MON)
        self.planning_service.replace_placements(MON, MON, [*existing, placement],
                                                 expected_versions={p.id: p.version for p in existing})
        return task, self.planning_service.get_placement(placement.id)

    def count(self, table: str) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def value(self, result):
        assert result.ok, result.error
        return result.value


@pytest.fixture
def stack(tmp_path: Path):
    s = Stack(tmp_path)
    yield s
    s.connection.close()


def test_viewing_writes_nothing_and_repeated_actions_never_duplicate(stack: Stack) -> None:
    task, placement = stack.planned()
    assert stack.value(stack.executions.executions_for_placements([placement.id])) == {}
    assert stack.count("executions") == 0 and stack.count("sync_dirty") == 3  # only the task, its type and the placement

    stack.clock.now = at(9)
    attempt = stack.value(stack.executions.get_or_create_canonical_execution(task, placement))
    started = stack.value(stack.executions.start(attempt.id, expected_version=attempt.version))
    assert stack.value(stack.executions.get_or_create_canonical_execution(task, placement)).id == attempt.id
    second = stack.executions.start(attempt.id, expected_version=started.version)
    assert not second.ok and "not possible any more" in second.error
    stale = stack.executions.pause(attempt.id, expected_version=attempt.version)
    assert not stale.ok and "changed elsewhere" in stale.error
    assert stack.count("executions") == 1 and stack.count("work_sessions") == 1
    assert stack.value(stack.executions.get_execution(started.id)).status == ExecutionStatus.IN_PROGRESS


def test_cancel_and_skip_work_on_a_placement_without_an_execution(stack: Stack) -> None:
    task_a, first = stack.planned("A", 9)
    task_b, second = stack.planned("B", 11)
    first_attempt = stack.execution_service.get_or_create_canonical_execution(task_a, first)
    cancelled = stack.execution_service.cancel(first_attempt.id, expected_version=first_attempt.version)
    skipped = stack.value(stack.executions.set_outcome(task_b, second, "uncompleted", expected_version=None))
    skipped = stack.execution_service.record_feedback(skipped.id, expected_version=skipped.version, note="not today")
    assert cancelled.status == ExecutionStatus.CANCELLED and skipped.note == "not today"


def test_reschedule_moves_unstarted_work_and_explains_refusals(stack: Stack) -> None:
    task, placement = stack.planned("Move me", 9)
    moved = stack.value(stack.planning.reschedule_placement(
        placement.id, expected_version=placement.version, planned_date=MON, start_minute=14 * 60,
        duration_minutes=60))
    assert moved.replacement.planned_start == at(14) and moved.previous.planned_start == at(9)
    stale = stack.planning.reschedule_placement(placement.id, expected_version=placement.version, planned_date=MON,
                                                start_minute=16 * 60, duration_minutes=60)
    assert not stale.ok and "moved, changed or removed elsewhere" in stale.error

    other_task, other = stack.planned("Blocker", 12)
    overlap = stack.planning.reschedule_placement(moved.replacement.id, expected_version=1, planned_date=MON,
                                                  start_minute=12 * 60 + 30, duration_minutes=60)
    assert not overlap.ok and overlap.error.startswith("It cannot be moved there")

    stack.clock.now = at(12)
    attempt = stack.execution_service.get_or_create_canonical_execution(other_task, other)
    stack.execution_service.start(attempt.id, expected_version=attempt.version)
    protected = stack.planning.reschedule_placement(other.id, expected_version=other.version, planned_date=MON,
                                                    start_minute=17 * 60, duration_minutes=60)
    assert not protected.ok and "stays in history" in protected.error and "in progress" in protected.error


def test_analytics_and_history_follow_the_persisted_actions(stack: Stack) -> None:
    task, placement = stack.planned("Same name", 9)
    duplicate, other = stack.planned("Same name", 11)
    stack.clock.now = at(13)
    before = stack.productivity.build_schedule_cohort_report(start_date=MON, end_date=MON)
    assert (before.due_completion.numerator, before.due_completion.denominator) == (0, 2)

    stack.clock.now = at(13, 5)
    attempt = stack.execution_service.get_or_create_canonical_execution(task, placement)
    started = stack.execution_service.start(attempt.id, expected_version=attempt.version)
    stack.clock.now = at(13, 35)
    finished = stack.execution_service.complete(started.id, expected_version=started.version)
    after = stack.productivity.build_schedule_cohort_report(start_date=MON, end_date=MON)
    assert (after.due_completion.numerator, after.due_completion.denominator) == (1, 2)

    assert after.timezone == TZ
    by_placement = {entry.placement_id: entry for entry in after.occurrences}
    assert set(by_placement) == {placement.id, other.id}
    assert by_placement[other.id].task_id == duplicate.id and by_placement[other.id].state.value == "not_started"
    assert by_placement[placement.id].state.value == "completed"
    assert by_placement[placement.id].actual_active_minutes == 30.0
    assert len(stack.execution_service.list_sessions(finished.id)) == 1


def test_history_keeps_moved_and_removed_work(stack: Stack) -> None:
    task, placement = stack.planned("Moved", 9)
    removed_task, removed = stack.planned("Removed", 11)
    moved = stack.value(stack.planning.reschedule_placement(
        placement.id, expected_version=placement.version, planned_date=MON, start_minute=15 * 60,
        duration_minutes=60))
    stack.planning_service.delete_task(removed_task.id, expected_version=removed_task.version)
    stack.clock.now = at(18)
    report = stack.productivity.build_schedule_cohort_report(start_date=MON, end_date=MON)
    [occurrence] = report.occurrences
    assert occurrence.original_placement_id == placement.id and occurrence.placement_id == moved.replacement.id
    assert occurrence.reschedule_events == 1 and occurrence.plan_timezone == TZ
    tombstone = stack.planning_service.get_placement(removed.id, include_deleted=True)
    assert tombstone.removal_reason.value == "task_deleted"
