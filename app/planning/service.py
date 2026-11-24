"""
app/planning/service.py

Build the day engine's input from one date of an allocation. The engine
returns minute-level placements for that date. The planning controller
uses SelectedDayState to present freshness derived from persisted
generation provenance (app/planning/provenance.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import date as date_
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
