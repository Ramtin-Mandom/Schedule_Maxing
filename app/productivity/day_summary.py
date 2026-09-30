"""
app/productivity/day_summary.py

Per-date aggregates of SCHEDULED work and the one classification of a
day's outcome -- the shared rules behind the Week/Month historical colours,
the selected-day panel, the bulk actions' reports, the server's
GET /days/summary, and later productivity analytics. Pure functions over
already-loaded records (no storage, no Tk), so every client and the server
compute exactly the same thing.

What counts: only a date's live placements -- tasks the scheduler actually
placed on it. A task it could not place was never scheduled and affects no
count, minute, point or percentage. Each placement's outcome is its
execution's (app/execution/lifecycle.outcome_of): no execution / scheduled /
in progress / paused = pending, completed = completed, skipped (or a
cancelled attempt) = uncompleted.

Planned vs actual stay apart: *_minutes are planned minutes (each
placement's own interval -- answered placements are never moved by a
re-run, so these stay put); completed_actual_minutes sums the recorded
active time of completions that were timed (timed_completed_count of
them); a completion reported without timing has no actual duration.

Points: an answered placement counts its execution's points snapshot (the
task's points when the attempt was recorded), so editing a task later never
rewrites what past work was worth; a placement without a snapshot (pending,
or an execution older than points) counts its task's current points.

Classification (classify_day), one deterministic precedence; percentages
are of scheduled_count, compared exactly (integer arithmetic, no float
rounding at the thresholds):

    1. scheduled_count == 0      NO_TASKS
    2. uncompleted >= 80%        MOSTLY_UNCOMPLETED_STRONG   (dark red)
    3. completed   >= 80%        MOSTLY_COMPLETED_STRONG     (dark green)
    4. uncompleted >= 60%        MOSTLY_UNCOMPLETED          (light red)
    5. completed   >= 60%        MOSTLY_COMPLETED            (light green)
    6. pending     >= 50%        MOSTLY_PENDING              (light white)
    7. otherwise                 MIXED                       (yellow)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date as date_
from enum import Enum

from app.execution.lifecycle import TaskOutcome, outcome_of
from app.execution.models import ExecutionStatus, TaskExecution


class DayStatusClass(str, Enum):
    NO_TASKS = "no_tasks"
    MOSTLY_UNCOMPLETED_STRONG = "mostly_uncompleted_strong"
    MOSTLY_COMPLETED_STRONG = "mostly_completed_strong"
    MOSTLY_UNCOMPLETED = "mostly_uncompleted"
    MOSTLY_COMPLETED = "mostly_completed"
    MOSTLY_PENDING = "mostly_pending"
    MIXED = "mixed"


#: (colour name, what it means) of each class, in the precedence order -- for legends, tooltips and screen readers.
DAY_STATUS_TEXT: dict[DayStatusClass, tuple[str, str]] = {
    DayStatusClass.NO_TASKS: ("Neutral, dark tint", "No scheduled tasks"),
    DayStatusClass.MOSTLY_UNCOMPLETED_STRONG: ("Dark red", "80% or more uncompleted"),
    DayStatusClass.MOSTLY_COMPLETED_STRONG: ("Dark green", "80% or more completed"),
    DayStatusClass.MOSTLY_UNCOMPLETED: ("Light red", "60% or more uncompleted"),
    DayStatusClass.MOSTLY_COMPLETED: ("Light green", "60% or more completed"),
    DayStatusClass.MOSTLY_PENDING: ("Light white", "50% or more still pending"),
    DayStatusClass.MIXED: ("Yellow", "Mixed: none of the thresholds above"),
}

#: A few words for a calendar cell (the colour is never the only signal).
DAY_STATUS_SHORT: dict[DayStatusClass, str] = {
    DayStatusClass.NO_TASKS: "no tasks",
    DayStatusClass.MOSTLY_UNCOMPLETED_STRONG: "≥80% uncompleted",
    DayStatusClass.MOSTLY_COMPLETED_STRONG: "≥80% completed",
    DayStatusClass.MOSTLY_UNCOMPLETED: "≥60% uncompleted",
    DayStatusClass.MOSTLY_COMPLETED: "≥60% completed",
    DayStatusClass.MOSTLY_PENDING: "≥50% pending",
    DayStatusClass.MIXED: "mixed",
}


def _at_least(part: int, whole: int, numerator: int, denominator: int) -> bool:
    """part / whole >= numerator / denominator, exactly."""
    return part * denominator >= numerator * whole


def classify_day(completed: int, uncompleted: int, pending: int) -> DayStatusClass:
    """The one classification of a day from its scheduled tasks' outcomes (see the module docstring)."""
    if min(completed, uncompleted, pending) < 0:
        raise ValueError("counts cannot be negative")
    scheduled = completed + uncompleted + pending
    if scheduled == 0:
        return DayStatusClass.NO_TASKS
    if _at_least(uncompleted, scheduled, 4, 5):
        return DayStatusClass.MOSTLY_UNCOMPLETED_STRONG
    if _at_least(completed, scheduled, 4, 5):
        return DayStatusClass.MOSTLY_COMPLETED_STRONG
    if _at_least(uncompleted, scheduled, 3, 5):
        return DayStatusClass.MOSTLY_UNCOMPLETED
    if _at_least(completed, scheduled, 3, 5):
        return DayStatusClass.MOSTLY_COMPLETED
    if _at_least(pending, scheduled, 1, 2):
        return DayStatusClass.MOSTLY_PENDING
    return DayStatusClass.MIXED


@dataclass(frozen=True)
class DaySummary:
    date: date_
    scheduled_count: int = 0
    completed_count: int = 0
    uncompleted_count: int = 0
    pending_count: int = 0
    scheduled_minutes: int = 0
    completed_minutes: int = 0
    uncompleted_minutes: int = 0
    pending_minutes: int = 0
    points_scheduled: int = 0
    points_completed: int = 0
    points_uncompleted: int = 0
    points_pending: int = 0
    #: Recorded active time of the completions that were timed, and how many of them there were.
    completed_actual_minutes: float = 0.0
    timed_completed_count: int = 0

    @property
    def status_class(self) -> DayStatusClass:
        return classify_day(self.completed_count, self.uncompleted_count, self.pending_count)

    @property
    def scheduled_hours(self) -> float:
        return round(self.scheduled_minutes / 60, 2)

    @property
    def completed_hours(self) -> float:
        return round(self.completed_minutes / 60, 2)

    @property
    def uncompleted_hours(self) -> float:
        return round(self.uncompleted_minutes / 60, 2)

    def percent(self, outcome: TaskOutcome) -> float | None:
        """The share of scheduled tasks with this outcome, 0-100 (None when nothing was scheduled)."""
        if not self.scheduled_count:
            return None
        count = {TaskOutcome.COMPLETED: self.completed_count, TaskOutcome.UNCOMPLETED: self.uncompleted_count,
                 TaskOutcome.PENDING: self.pending_count}[TaskOutcome(outcome)]
        return round(100 * count / self.scheduled_count, 1)

    def as_dict(self) -> dict:
        """The aggregates as plain JSON-friendly values (the REST representation)."""
        data = asdict(self)
        data["date"] = self.date.isoformat()
        data["status_class"] = self.status_class.value
        return data


def summarize_day(day: date_, placements: Iterable, tasks: Mapping[uuid.UUID, object],
                  executions: Mapping[uuid.UUID, TaskExecution]) -> DaySummary:
    """
    The aggregates of one date from its live placements (ScheduledTask),
    their tasks (by id; a missing task counts 0 points) and the live
    executions of those placements (by placement id).
    """
    totals = dict(scheduled_count=0, completed_count=0, uncompleted_count=0, pending_count=0, scheduled_minutes=0,
                  completed_minutes=0, uncompleted_minutes=0, pending_minutes=0, points_scheduled=0,
                  points_completed=0, points_uncompleted=0, points_pending=0, completed_actual_minutes=0.0,
                  timed_completed_count=0)
    for placement in placements:
        if placement.planned_date != day or getattr(placement, "deleted_at", None) is not None:
            continue
        execution = executions.get(placement.id)
        status = ExecutionStatus(execution.status) if execution is not None else None
        outcome = outcome_of(status)
        minutes = round((placement.planned_end - placement.planned_start).total_seconds() / 60)
        task = tasks.get(placement.task_id)
        snapshot = execution.points if execution is not None else None
        points = snapshot if snapshot is not None else int(getattr(task, "points", 0) or 0)
        key = {TaskOutcome.COMPLETED: "completed", TaskOutcome.UNCOMPLETED: "uncompleted",
               TaskOutcome.PENDING: "pending"}[outcome]
        totals["scheduled_count"] += 1
        totals[f"{key}_count"] += 1
        totals["scheduled_minutes"] += minutes
        totals[f"{key}_minutes"] += minutes
        totals["points_scheduled"] += points
        totals[f"points_{key}"] += points
        if outcome == TaskOutcome.COMPLETED and execution.actual_active_duration_minutes is not None:
            totals["completed_actual_minutes"] += execution.actual_active_duration_minutes
            totals["timed_completed_count"] += 1
    totals["completed_actual_minutes"] = round(totals["completed_actual_minutes"], 2)
    return DaySummary(date=day, **totals)


def summarize_range(days: Iterable[date_], placements: Iterable, tasks: Mapping[uuid.UUID, object],
                    executions: Mapping[uuid.UUID, TaskExecution]) -> dict[date_, DaySummary]:
    """summarize_day for every date of `days` from one load of records (no per-date reads)."""
    by_day: dict[date_, list] = {day: [] for day in days}
    for placement in placements:
        if placement.planned_date in by_day:
            by_day[placement.planned_date].append(placement)
    return {day: summarize_day(day, items, tasks, executions) for day, items in by_day.items()}
