"""The one classification of a day and its scheduled-work aggregates (app/productivity/day_summary.py):
every threshold exactly at and just under its boundary, the deterministic precedence where
thresholds overlap, and counts/minutes/points built only from a date's live placements -- with
an answered attempt's points snapshot, never the optimizer's placement score."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus, TaskExecution
from app.planning.models import ScheduledTask, Task
from app.productivity.day_summary import (
    DAY_STATUS_SHORT,
    DAY_STATUS_TEXT,
    DayStatusClass,
    classify_day,
    summarize_day,
    summarize_range,
)

C = DayStatusClass
DAY = date(2026, 9, 21)


@pytest.mark.parametrize("completed, uncompleted, pending, expected", [
    (0, 0, 0, C.NO_TASKS),
    (10, 0, 0, C.MOSTLY_COMPLETED_STRONG),  # 100% completed
    (8, 0, 2, C.MOSTLY_COMPLETED_STRONG),  # exactly 80%
    (79, 0, 21, C.MOSTLY_COMPLETED),  # 79%: not strong
    (6, 0, 4, C.MOSTLY_COMPLETED),  # exactly 60%
    (59, 0, 41, C.MIXED),  # 59% completed, 41% pending: none applies
    (0, 10, 0, C.MOSTLY_UNCOMPLETED_STRONG),  # 100% uncompleted
    (1, 8, 1, C.MOSTLY_UNCOMPLETED_STRONG),  # exactly 80%
    (0, 79, 21, C.MOSTLY_UNCOMPLETED),  # 79%
    (2, 6, 2, C.MOSTLY_UNCOMPLETED),  # exactly 60%
    (0, 59, 41, C.MIXED),  # 59% uncompleted
    (0, 0, 10, C.MOSTLY_PENDING),  # nothing answered yet
    (3, 2, 5, C.MOSTLY_PENDING),  # exactly 50% pending
    (3, 3, 4, C.MIXED),  # 40% pending
    (4, 4, 2, C.MIXED),  # a genuinely mixed day: yellow
    (1, 1, 1, C.MIXED),
    (2, 1, 0, C.MOSTLY_COMPLETED),  # 66.7%
    (1, 2, 0, C.MOSTLY_UNCOMPLETED),
])
def test_every_threshold_and_boundary(completed, uncompleted, pending, expected) -> None:
    assert classify_day(completed, uncompleted, pending) == expected


def test_overlapping_thresholds_follow_the_one_precedence() -> None:
    # 60% pending and 40% uncompleted: only "pending" qualifies. 60% completed and 40% pending: completed wins.
    assert classify_day(0, 4, 6) == C.MOSTLY_PENDING
    assert classify_day(6, 0, 4) == C.MOSTLY_COMPLETED
    # Uncompleted is checked before completed at the same strength, but both cannot reach 80% together;
    # at 60% uncompleted vs 40% completed the red class wins (it comes first).
    assert classify_day(4, 6, 0) == C.MOSTLY_UNCOMPLETED
    # A strong class always beats a lighter one: 80% completed is dark green even with 20% uncompleted.
    assert classify_day(8, 2, 0) == C.MOSTLY_COMPLETED_STRONG
    # Exact arithmetic at the edges: 4/5 is 80% (not 79.99...), 3/5 is 60%.
    assert classify_day(4, 1, 0) == C.MOSTLY_COMPLETED_STRONG and classify_day(3, 2, 0) == C.MOSTLY_COMPLETED
    with pytest.raises(ValueError):
        classify_day(-1, 0, 0)


def test_every_class_has_words_not_only_a_colour() -> None:
    assert set(DAY_STATUS_TEXT) == set(DAY_STATUS_SHORT) == set(DayStatusClass)
    assert DAY_STATUS_TEXT[C.MIXED][0] == "Yellow" and DAY_STATUS_TEXT[C.NO_TASKS][1] == "No scheduled tasks"


# -----------------------------------------------------------------------------
# Aggregates
# -----------------------------------------------------------------------------


def task(name: str, points: int, minutes: int = 60) -> Task:
    return Task(name=name, category="study", estimated_duration_minutes=minutes, priority=5, points=points)


def placement(item: Task, hour: int, minutes: int = 60, day: date = DAY, score: float = 99.0) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=item.id, planned_date=day, timezone="UTC", planned_start=start,
                         planned_end=start + timedelta(minutes=minutes), score=score)


def execution(of: ScheduledTask, status: ExecutionStatus, points: int | None, active: float | None = None) -> TaskExecution:
    now = "2026-09-21T20:00:00+00:00"
    return TaskExecution(id=str(uuid.uuid4()), task_name="x", category="study", tag="", planned_duration=60, priority=5,
                         status=status, created_at=now, updated_at=now, scheduled_task_id=of.id, points=points,
                         actual_active_duration_minutes=active)


def test_counts_minutes_and_points_of_scheduled_work_only() -> None:
    a, b, c, d, e = task("A", 5), task("B", 3), task("C", 2), task("D", 4), task("E", 1)
    pa, pb, pc, pd = placement(a, 8, 90), placement(b, 10, 30), placement(c, 12, 45), placement(d, 14, 60)
    removed = placement(e, 16).model_copy(update={"deleted_at": datetime(2026, 9, 21, tzinfo=timezone.utc)})
    tasks = {t.id: t for t in (a, b, c, d, e)}
    executions = {
        pa.id: execution(pa, ExecutionStatus.COMPLETED, points=7, active=80.0),  # the snapshot, not today's 5
        pb.id: execution(pb, ExecutionStatus.SKIPPED, points=None),  # an older execution: the task's points
        pc.id: execution(pc, ExecutionStatus.COMPLETED, points=2),  # completed without timing
        # D has no execution: pending. E's placement is a tombstone: not scheduled any more.
    }
    summary = summarize_day(DAY, [pa, pb, pc, pd, removed], tasks, executions)
    assert (summary.scheduled_count, summary.completed_count, summary.uncompleted_count, summary.pending_count) == (
        4, 2, 1, 1)
    assert (summary.scheduled_minutes, summary.completed_minutes, summary.uncompleted_minutes,
            summary.pending_minutes) == (225, 135, 30, 60)
    assert summary.scheduled_hours == 3.75 and summary.completed_hours == 2.25
    assert (summary.points_scheduled, summary.points_completed, summary.points_uncompleted, summary.points_pending) == (
        16, 9, 3, 4)
    assert summary.completed_actual_minutes == 80.0 and summary.timed_completed_count == 1  # planned != actual
    assert summary.status_class == C.MIXED and summary.percent(TaskOutcome.COMPLETED) == 50.0
    data = summary.as_dict()
    assert data["date"] == "2026-09-21" and data["status_class"] == "mixed" and data["points_completed"] == 9
    assert "score" not in str(data)  # the optimizer's placement score is not a productivity value


def test_an_unscheduled_task_or_an_empty_day_counts_nothing() -> None:
    lonely = task("Never placed", 50)
    summaries = summarize_range([DAY, DAY + timedelta(days=1)], [], {lonely.id: lonely}, {})
    assert summaries[DAY].scheduled_count == 0 and summaries[DAY].points_scheduled == 0
    assert summaries[DAY].status_class == C.NO_TASKS and summaries[DAY].percent(TaskOutcome.COMPLETED) is None
