"""
app/planning/allocation.py

Lightweight, deterministic task-to-date allocation for a week or month
(Task 5 / Schedule Maxing v2). Allocation decides *which date* each task
goes on -- it never decides a minute-level start/end time, and it never
calls app.optimizer.generate_day_schedule (the Day Scheduler); see
tests/test_allocation.py's spy test for a hard guarantee of that.

Allocation output is a planning-level fact ("this task is assigned to this
date"), not proof that a detailed intra-day schedule exists for it --
app.planning.service.generate_selected_day is what actually calls the Day
Scheduler for one specific date, and can still fail (a coarse-feasible day
can be detailed-infeasible; see that module and its tests).

Calendar coverage: week_dates/month_dates use real calendar arithmetic
(datetime.date + calendar.monthrange), so a "month" naturally has the
correct number of days including leap Februarys, and a date range may
freely cross a year boundary -- there is no assumption of a fixed 28/30/31.

Recurrence stays model-only here too: allocate_tasks takes concrete Task
instances (each with its own id) and never expands a RecurrenceSpec into
occurrences; a task whose `recurrence` is set but that has not itself been
materialized into concrete occurrences elsewhere is allocated exactly as
given, like any other single task.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, Field

from app.planning.models import FixedBlock, Task, TaskRegistry
from app.planning.preferences import DayPreferences
from app.planning.time import (
    MINUTES_PER_DAY,
    AmbiguousLocalTimeError,
    UnsupportedSchedulingWindowError,
    local_date_of,
)


def week_dates(start_date: date_) -> list[date_]:
    """The 7 actual calendar dates of the week beginning at `start_date`."""
    return [start_date + timedelta(days=offset) for offset in range(7)]


def month_dates(year: int, month: int) -> list[date_]:
    """Every actual calendar date in `year`-`month`, first to last (28-31
    days as appropriate, including leap Februarys)."""
    _, days_in_month = calendar.monthrange(year, month)
    return [date_(year, month, day) for day in range(1, days_in_month + 1)]


class AllocationReasonCode(str, Enum):
    CAPACITY_EXCEEDED = "capacity_exceeded"
    REQUIRED_DATE_CONFLICT = "required_date_conflict"
    DEADLINE_INFEASIBLE = "deadline_infeasible"
    NO_FEASIBLE_DATE = "no_feasible_date"


class UnallocatedEntry(BaseModel):
    task_id: uuid.UUID
    reason_code: AllocationReasonCode
    explanation: str = Field(min_length=1)
    required: bool
    #: True only when a coarse bound proves no date in [start_date, end_date]
    #: could ever work (e.g. required_date outside the range, deadline
    #: before the earliest possible date, or aggregate capacity provably
    #: insufficient). False means this greedy allocation pass did not find
    #: a date -- not a proof that none exists. Mirrors
    #: app.optimizer.MandatoryTaskFailure.proven_infeasible.
    proven_infeasible: bool = False


class AllocationDiagnostic(BaseModel):
    """A non-fatal note about allocation input, carried through to the caller."""

    task_id: uuid.UUID | None = None
    message: str = Field(min_length=1)


class AllocationResult(BaseModel):
    """
    The result of one allocation run.

    `id` is this run's own identity -- app.planning.service uses it to
    detect whether a previously generated day's task set is still current
    (see that module's invalidation policy). `assignments` maps every
    successfully allocated task_id to its assigned date; a task_id present
    in `tasks` but not in `assignments` is in `unallocated` instead. No
    ScheduledTask placements or start/end timestamps are produced here.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    start_date: date_
    end_date: date_
    assignments: dict[uuid.UUID, date_] = Field(default_factory=dict)
    #: Remaining free minutes per date *after* every successful assignment
    #: (fixed-block occupancy already subtracted from each date's total).
    capacity_remaining: dict[date_, int] = Field(default_factory=dict)
    unallocated: list[UnallocatedEntry] = Field(default_factory=list)
    diagnostics: list[AllocationDiagnostic] = Field(default_factory=list)


def _day_window_total_minutes(day_window) -> int:
    return day_window.end_day_offset * MINUTES_PER_DAY + day_window.end_minute - day_window.start_minute


def _fixed_minutes_for_date(fixed_blocks: list[FixedBlock]) -> int:
    return sum(round((block.planned_end - block.planned_start).total_seconds() / 60) for block in fixed_blocks)


def _validate_fixed_blocks_no_overlap(fixed_blocks: list[FixedBlock], on_date: date_) -> None:
    ordered = sorted(fixed_blocks, key=lambda block: block.planned_start)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.planned_end > later.planned_start:
            raise ValueError(f"Overlapping fixed blocks on {on_date}: {earlier.label!r} and {later.label!r}.")


def _allocation_order(task_ids: set[uuid.UUID], registry: dict[uuid.UUID, Task]) -> list[uuid.UUID]:
    """
    The order a tier's tasks claim dates in (earlier tasks get the better
    dates while capacity lasts): higher points first -- the user's own value
    of a task -- then the earliest deadline (none last), then the longer task
    (harder to fit later), then id, so the result is deterministic.
    """
    far = datetime.max.replace(tzinfo=timezone.utc)

    def key(task_id: uuid.UUID) -> tuple:
        task = registry[task_id]
        return (-task.points, task.deadline or far, -task.estimated_duration_minutes, str(task_id))

    return sorted(task_ids, key=key)


def _rank_candidate_dates(
    task: Task,
    candidate_dates: list[date_],
    preferences_by_date: dict[date_, DayPreferences],
    capacity_remaining: dict[date_, int],
) -> list[date_]:
    """
    Deterministic ranking (best first) among feasible candidate dates:
        1. a date in task.preferred_dates beats one that is not;
        2. higher effective_category_multiplier for task.category on that
           date beats lower (the day's own preference signal for this
           category);
        3. more remaining free capacity on that date beats less (spreads
           load rather than always packing the earliest day solid);
        4. earliest date wins any remaining tie -- the final, deterministic
           tie-break.
    Required-task urgency is handled by the caller (required tasks are
    processed before optional ones) rather than as a per-date ranking
    factor here.
    """

    def sort_key(candidate: date_) -> tuple:
        preferred = 0 if candidate in task.preferred_dates else 1
        category_multiplier = preferences_by_date[candidate].effective_category_multiplier(task.category)
        remaining = capacity_remaining.get(candidate, 0)
        return (preferred, -category_multiplier, -remaining, candidate.toordinal())

    return sorted(candidate_dates, key=sort_key)


def _deadline_allows(task: Task, day: date_, preferences: DayPreferences) -> bool:
    """
    A coarse, necessary condition for placing `task` on `day` before its
    deadline, judged in that date's own planning timezone (never the
    deadline's UTC calendar date): the deadline's local date is not before
    `day`, and the deadline leaves at least the task's duration after the
    day window starts. Meeting it proves nothing -- the day engine's
    intraday deadline constraint stays authoritative. When the window's
    start is itself ambiguous or nonexistent local time (DST), only the date
    rule applies; the day engine refuses such a window explicitly.
    """
    if local_date_of(task.deadline, preferences.timezone) < day:
        return False
    try:
        window_start, _ = preferences.to_local_day_window().endpoint_instants()
    except (AmbiguousLocalTimeError, UnsupportedSchedulingWindowError):
        return True
    return window_start + timedelta(minutes=task.estimated_duration_minutes) <= task.deadline


def _feasible_dates_for_task(
    task: Task,
    all_dates: list[date_],
    capacity_remaining: dict[date_, int],
    preferences_by_date: dict[date_, DayPreferences],
) -> tuple[list[date_], bool]:
    """
    Returns (feasible_dates, capacity_was_the_only_blocker). The second
    value distinguishes "no date has enough remaining capacity" (a
    capacity-only rejection, potentially transient as other tasks free up
    ranking but not capacity itself) from a harder rejection (required_date/
    deadline eliminating dates outright) for reason-code purposes.
    """
    if task.required_date is not None:
        candidates = [task.required_date] if task.required_date in all_dates else []
    elif task.deadline is not None:
        candidates = [d for d in all_dates if _deadline_allows(task, d, preferences_by_date[d])]
    else:
        candidates = list(all_dates)

    capacity_only = bool(candidates)
    feasible = [d for d in candidates if capacity_remaining.get(d, 0) >= task.estimated_duration_minutes]
    return feasible, capacity_only


def allocate_tasks(
    *,
    start_date: date_,
    end_date: date_,
    tasks: TaskRegistry,
    task_ids: list[uuid.UUID],
    preferences_by_date: dict[date_, DayPreferences],
    fixed_blocks_by_date: dict[date_, list[FixedBlock]] | None = None,
) -> AllocationResult:
    """
    Allocate `task_ids` (drawn from `tasks`) across the inclusive date range
    [start_date, end_date]. Never produces minute-level placements and
    never calls the Day Scheduler.

    Order: validate fixed blocks (once, up front), reserve fixed-block
    occupancy, then allocate the required tier before the optional tier,
    each in _allocation_order (points first). Every date, for every task, must respect:
    enough remaining free capacity; required_date/deadline (a deadline
    judged in each date's own planning timezone, see _deadline_allows).

    Required-allocation failure is reported in `unallocated` with
    `required=True` -- distinguishable from an optional task's entry -- and
    this function never raises for it; a caller that wants a hard failure
    can inspect `unallocated` itself. This deliberately differs from
    app.optimizer.generate_day_schedule's raising behavior: allocation
    operates at a coarser, best-effort planning granularity where a partial
    result (e.g. "11 of 12 required tasks fit this week") remains useful
    even when something does not fit.
    """
    all_dates = [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]
    fixed_blocks_by_date = fixed_blocks_by_date or {}

    for day in all_dates:
        _validate_fixed_blocks_no_overlap(fixed_blocks_by_date.get(day, []), day)

    registry = {task_id: tasks.get(task_id) for task_id in task_ids}
    if any(task is None for task in registry.values()):
        missing = [str(task_id) for task_id, task in registry.items() if task is None]
        raise ValueError(f"task_ids references tasks missing from the registry: {missing}")

    capacity_remaining: dict[date_, int] = {
        day: (
            _day_window_total_minutes(preferences_by_date[day].day_window)
            - _fixed_minutes_for_date(fixed_blocks_by_date.get(day, []))
        )
        for day in all_dates
    }

    required_order = _allocation_order({task_id for task_id in task_ids if registry[task_id].required}, registry)
    optional_order = _allocation_order({task_id for task_id in task_ids if not registry[task_id].required}, registry)

    assignments: dict[uuid.UUID, date_] = {}
    unallocated: list[UnallocatedEntry] = []

    def try_allocate(task_id: uuid.UUID, *, required: bool) -> None:
        task = registry[task_id]
        feasible, capacity_only = _feasible_dates_for_task(task, all_dates, capacity_remaining, preferences_by_date)

        if not feasible:
            if task.required_date is not None and task.required_date not in all_dates:
                unallocated.append(
                    UnallocatedEntry(
                        task_id=task_id, reason_code=AllocationReasonCode.REQUIRED_DATE_CONFLICT,
                        explanation=f"required_date {task.required_date} is outside [{start_date}, {end_date}].",
                        required=required, proven_infeasible=True,
                    )
                )
            elif task.deadline is not None and not any(
                _deadline_allows(task, day, preferences_by_date[day]) for day in all_dates
            ):
                unallocated.append(
                    UnallocatedEntry(
                        task_id=task_id, reason_code=AllocationReasonCode.DEADLINE_INFEASIBLE,
                        explanation=(
                            f"no date in range leaves {task.estimated_duration_minutes} "
                            f"minutes before its deadline ({task.deadline.isoformat()}) in the planning timezone."
                        ),
                        required=required, proven_infeasible=True,
                    )
                )
            elif capacity_only:
                unallocated.append(
                    UnallocatedEntry(
                        task_id=task_id, reason_code=AllocationReasonCode.CAPACITY_EXCEEDED,
                        explanation="no candidate date has enough remaining free capacity for this task's duration.",
                        required=required,
                    )
                )
            else:
                unallocated.append(
                    UnallocatedEntry(
                        task_id=task_id, reason_code=AllocationReasonCode.NO_FEASIBLE_DATE,
                        explanation="no date in range satisfies this task's constraints.",
                        required=required,
                    )
                )
            return

        chosen = _rank_candidate_dates(task, feasible, preferences_by_date, capacity_remaining)[0]
        assignments[task_id] = chosen
        capacity_remaining[chosen] -= task.estimated_duration_minutes

    for task_id in required_order:
        try_allocate(task_id, required=True)

    for task_id in optional_order:
        try_allocate(task_id, required=False)

    return AllocationResult(
        start_date=start_date,
        end_date=end_date,
        assignments=assignments,
        capacity_remaining=capacity_remaining,
        unallocated=unallocated,
    )
