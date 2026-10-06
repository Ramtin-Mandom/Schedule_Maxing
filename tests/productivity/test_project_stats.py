"""
A project's collected points (app/productivity/project_stats.py): the period and the average's denominator,
points by day, scoping to the project's tasks, an empty project, and -- through the tracker's own merge -- a task
completed both on a time slot and without one counted once.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.productivity.project_stats import build_project_points
from app.productivity.tracker import CompletionItem, merge_completions

TODAY = date(2026, 10, 5)
MINE, ALSO_MINE, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def done(task_id, day: date, points: int | None, *, placement: bool = False, hour: int = 12) -> CompletionItem:
    execution_id = str(uuid.uuid4())
    placement_id = uuid.uuid4() if placement else None
    return CompletionItem(
        execution_id=execution_id, occurrence_key=f"placement:{placement_id}" if placement else f"execution:{execution_id}",
        task_id=task_id, placement_id=placement_id,
        completed_at=datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc), local_date=day, points=points,
        active_minutes=None, name="Task", category="study", tags=None, type_id=None)


def test_total_by_day_and_average_over_every_calendar_day_of_the_period():
    items = [done(MINE, date(2026, 10, 1), 10), done(ALSO_MINE, date(2026, 10, 1), 5, placement=True),
             done(MINE, date(2026, 10, 4), 6), done(OTHER, date(2026, 10, 2), 100)]
    report = build_project_points(items, [MINE, ALSO_MINE], today=TODAY)
    assert (report.total_points, report.completed_count) == (21, 3)  # another project's task never counts
    assert [(day.date, day.points, day.completed_count) for day in report.by_day] == [
        (date(2026, 10, 1), 15, 2), (date(2026, 10, 4), 6, 1)]
    # All time starts on the project's first completion: Oct 1..5 is five days, the empty ones included.
    assert (report.period_start, report.period_end, report.days_in_period) == (date(2026, 10, 1), TODAY, 5)
    assert report.average_points_per_day == pytest.approx(4.2)


def test_a_date_range_is_the_last_n_days_and_is_the_denominator():
    items = [done(MINE, TODAY - timedelta(days=40), 50), done(MINE, TODAY - timedelta(days=6), 14),
             done(MINE, TODAY, None)]
    week = build_project_points(items, [MINE], today=TODAY, range_days=7)
    assert (week.period_start, week.days_in_period) == (TODAY - timedelta(days=6), 7)
    assert (week.total_points, week.completed_count, week.unknown_points_count) == (14, 2, 1)
    assert week.average_points_per_day == 2.0  # 14 / 7, not 14 / the 2 days something was completed on
    everything = build_project_points(items, [MINE], today=TODAY)
    assert everything.total_points == 64 and everything.days_in_period == 41
    with pytest.raises(ValueError):
        build_project_points(items, [MINE], today=TODAY, range_days=0)


def test_a_project_without_completions_has_no_period_and_no_invented_average():
    for range_days, days in ((None, 0), (30, 30)):
        report = build_project_points([done(OTHER, TODAY, 9)], [MINE], today=TODAY, range_days=range_days)
        assert not report.has_data and report.total_points == 0 and report.by_day == []
        assert report.days_in_period == days
    assert build_project_points([], [MINE], today=TODAY).average_points_per_day is None
    assert build_project_points([], [MINE], today=TODAY, range_days=30).average_points_per_day == 0.0


def test_a_task_completed_on_a_slot_and_also_directly_is_one_completion():
    scheduled = done(MINE, date(2026, 10, 2), 8, placement=True)
    direct = done(MINE, date(2026, 10, 3), 8)
    unrelated_direct = done(ALSO_MINE, date(2026, 10, 3), 4)
    merged, dropped = merge_completions([direct, scheduled, unrelated_direct])
    assert dropped == 1 and {item.execution_id for item in merged} == {scheduled.execution_id,
                                                                       unrelated_direct.execution_id}
    report = build_project_points(merged, [MINE, ALSO_MINE], today=TODAY)
    assert (report.total_points, report.completed_count) == (12, 2)  # 8 once, plus the other task's 4
