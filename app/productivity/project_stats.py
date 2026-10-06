"""
app/productivity/project_stats.py

The points a project collected, from the tracker's completion records
(app/productivity/tracker.py: one per completed occurrence, on the local date
it was completed, with the points snapshot of that completion). A task
completed from its project without a time slot is one of those records like
any other, and a task is never counted twice (tracker.merge_completions).

The reporting period follows the tracker's date-range convention:
`range_days` is the last N local dates ending today; None is all time, which
for a project starts on the date of its first completion. The average is the
period's total divided by every calendar day of the period -- days without a
completion included -- and the report states both.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from pydantic import BaseModel

from app.productivity.tracker import CompletionItem


class ProjectDayPoints(BaseModel):
    date: date
    points: int
    completed_count: int


class ProjectPointsReport(BaseModel):
    #: The last N local dates ending today (None: all time, from the project's first completion).
    range_days: int | None
    #: First and last date of the averaging period (start None: nothing was ever completed -- no period).
    period_start: date | None
    period_end: date
    #: The average's denominator: every calendar day of the period (0 without a period).
    days_in_period: int
    total_points: int
    completed_count: int
    #: Completions whose points were not recorded (older records); they count as completed, with 0 points.
    unknown_points_count: int
    #: Ascending; only the dates something was completed on.
    by_day: list[ProjectDayPoints]
    #: total_points / days_in_period (None without a period).
    average_points_per_day: float | None

    @property
    def has_data(self) -> bool:
        return self.completed_count > 0


def build_project_points(completions: Iterable[CompletionItem], task_ids: Iterable[uuid.UUID], *, today: date,
                         range_days: int | None = None) -> ProjectPointsReport:
    """The report of the tasks `task_ids` (a project's, deleted ones included) as of `today`. Pure."""
    if range_days is not None and range_days <= 0:
        raise ValueError("range_days must be positive (or None for all time)")
    own = set(task_ids)
    items = [item for item in completions if item.task_id in own and item.local_date <= today]
    if range_days is not None:
        start: date | None = today - timedelta(days=range_days - 1)
        items = [item for item in items if item.local_date >= start]
    else:
        start = min((item.local_date for item in items), default=None)
    points: dict[date, int] = defaultdict(int)
    counts: dict[date, int] = defaultdict(int)
    for item in items:
        points[item.local_date] += item.points or 0
        counts[item.local_date] += 1
    days = (today - start).days + 1 if start is not None else 0
    total = sum(points.values())
    return ProjectPointsReport(
        range_days=range_days, period_start=start, period_end=today, days_in_period=days, total_points=total,
        completed_count=len(items), unknown_points_count=sum(1 for item in items if item.points is None),
        by_day=[ProjectDayPoints(date=day, points=points[day], completed_count=counts[day]) for day in sorted(counts)],
        average_points_per_day=round(total / days, 2) if days else None,
    )
