"""
app/planning/service.py

The selected-day service (Task 5 / Schedule Maxing v2): the one public
operation that actually calls app.optimizer.generate_day_schedule (the Day
Scheduler), for exactly one selected date, using an AllocationResult's
assignments to determine which tasks belong to that date.

This module never optimizes any date other than the one explicitly
selected.

Result staleness: DayResultStatus/SelectedDayState provide a minimal,
explicit policy: a generated result is tied to the AllocationResult.id it
was generated from. (Since Milestone 3 the desktop/CLI controller derives
a date's state from persisted provenance instead -- see
app/planning/provenance.py -- so it survives restarts; mark_stale_if_outdated
remains the in-memory rule for callers that hold states themselves.) Any *new* allocation run (a different id -- produced
whenever the caller re-allocates after a task/preference/fixed-block edit)
makes every previously generated SelectedDayState stale, via
mark_stale_if_outdated. This is intentionally coarse (a fresh allocation
invalidates every generated day, not just the ones actually affected) --
safe by construction (it can only over-invalidate, never leave a stale
result looking current), and simple enough to reason about without a full
dependency-impact graph, which is out of scope for this milestone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import date as date_
from datetime import timedelta
from enum import Enum

from app.optimizer import generate_day_schedule
from app.planning.allocation import AllocationResult
from app.planning.models import DaySchedule, DayScheduleOutput, FixedBlock, TaskRegistry
from app.planning.preferences import DayPreferences
from app.planning.provenance import StaleReason


class DayResultStatus(str, Enum):
    #: Tasks are assigned to this date (via allocation) but no
    #: minute-level schedule has been generated for it yet.
    ALLOCATED = "allocated"
    #: generate_selected_day has produced a DayScheduleOutput for this date,
    #: and it is still tied to the allocation it came from.
    GENERATED = "generated"
    #: A DayScheduleOutput exists, but a newer allocation run supersedes the
    #: one it was generated from -- it may no longer reflect the current
    #: task/preference/fixed-block state and must not be presented as current.
    STALE = "stale"


@dataclass(frozen=True)
class SelectedDayState:
    """The generation status for one date, tracked by the allocation run
    (by id) it reflects."""

    date: date_
    status: DayResultStatus
    result: DayScheduleOutput | None
    generated_from_allocation_id: uuid.UUID | None
    #: Why a STALE state is stale (None for other statuses, or when unknown).
    stale_reason: StaleReason | None = None


def initial_state(day: date_) -> SelectedDayState:
    return SelectedDayState(date=day, status=DayResultStatus.ALLOCATED, result=None, generated_from_allocation_id=None)


def mark_stale_if_outdated(state: SelectedDayState, current_allocation_id: uuid.UUID) -> SelectedDayState:
    """
    Return `state` unchanged if it is not GENERATED, or if it was generated
    from the current allocation run; otherwise return a new STALE state
    (the prior result is kept, only relabeled, so a caller can still show
    "this was the last schedule, but it is out of date" rather than losing
    it outright).
    """
    if state.status == DayResultStatus.GENERATED and state.generated_from_allocation_id != current_allocation_id:
        return SelectedDayState(
            date=state.date, status=DayResultStatus.STALE, result=state.result,
            generated_from_allocation_id=state.generated_from_allocation_id,
            stale_reason=StaleReason.SUPERSEDED_ALLOCATION,
        )
    return state


def generate_selected_day(
    allocation: AllocationResult,
    selected_date: date_,
    tasks: TaskRegistry,
    preferences_by_date: dict[date_, DayPreferences],
    fixed_blocks_by_date: dict[date_, list[FixedBlock]] | None = None,
    *,
    previous_result: DayScheduleOutput | None = None,
    task_bonuses: Mapping[uuid.UUID, float] | None = None,
) -> tuple[DayScheduleOutput, SelectedDayState]:
    """
    Generate the minute-level schedule for exactly `selected_date`, from
    the tasks `allocation` assigned to it. Calls
    app.optimizer.generate_day_schedule exactly once. Does not optimize
    any other date, and does not re-run allocation itself.

    Raises app.optimizer.MandatoryTaskSchedulingError under the same
    conditions generate_day_schedule itself does -- a coarse-feasible
    allocation (aggregate capacity worked out) does not guarantee a
    detailed intra-day schedule exists; that distinction is deliberate and
    this function does not hide it.
    """
    fixed_blocks_by_date = fixed_blocks_by_date or {}
    preferences = preferences_by_date[selected_date]

    todays_task_ids = [task_id for task_id, assigned_date in allocation.assignments.items() if assigned_date == selected_date]

    day_schedule = DaySchedule(
        date=selected_date,
        timezone=preferences.timezone,
        fixed_blocks=fixed_blocks_by_date.get(selected_date, []),
        task_ids=todays_task_ids,
        tasks=tasks,
    )

    result = generate_day_schedule(
        day_schedule, preferences, previous_result=previous_result, task_bonuses=task_bonuses,
    )

    state = SelectedDayState(
        date=selected_date, status=DayResultStatus.GENERATED, result=result, generated_from_allocation_id=allocation.id
    )
    return result, state


def week_states(start_date: date_) -> dict[date_, SelectedDayState]:
    """Initial ALLOCATED-or-nothing state for every date in a week, before
    any date has been generated -- a small convenience for a caller
    building its own per-date state map."""
    return {start_date + timedelta(days=offset): initial_state(start_date + timedelta(days=offset)) for offset in range(7)}
