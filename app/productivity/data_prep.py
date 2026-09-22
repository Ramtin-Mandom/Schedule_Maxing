"""
data_prep.py

Turns raw TaskExecution rows (from app/execution/repository.py) into flat,
analysis-ready Observation records. This is the only module in
app/productivity/ (besides the repository itself) that touches the
execution repository -- stats.py, segments.py, prediction.py, and
insights.py all operate purely on lists of Observation and never see a
repository or a database connection.

Legacy vs. canonical basis (Task 2 / Schedule Maxing v2):
    TaskExecution rows are either legacy (planned_date/planned_start/
    planned_end are the abstract day-index/minutes-from-midnight snapshot)
    or canonical (task_id is set; canonical_planned_date/canonical_timezone/
    canonical_planned_start/canonical_planned_end carry a real aware planned
    date/instant instead -- see app/execution/models.py's module docstring).
    Observation exposes a single, uniform view over both:
        - planned_start/planned_end (int, minutes-from-midnight): the
          record's own legacy values when present, otherwise the local
          wall-clock minute-of-day derived from canonical_planned_start/end
          in canonical_timezone. Every existing consumer (stats.py,
          ml_features.py, duration_suggestion.py) keeps reading a plain
          minute-of-day without needing to know which kind of row produced
          it.
        - effective_planned_date (date | None): the real calendar date when
          known (canonical_planned_date), else None -- never guessed for a
          legacy row (there is no recoverable calendar anchor for an
          abstract day index).
        - day_of_week: derived from effective_planned_date when known (the
          task's actual planned weekday); otherwise falls back to the
          original created_at-based approximation
          (day_of_week_for_timestamp) -- unchanged legacy behavior,
          documented in buckets.py.
    The `days`/`period` window filter below is unaffected by any of this: it
    keeps comparing against created_at for every record, legacy or
    canonical, per Task 2's "preserve the documented created_at basis for
    existing recent-records filters" requirement.

Plausibility filtering:
    MAX_PLAUSIBLE_DURATION_MINUTES bounds actual_active_duration_minutes at
    one full day (1440 minutes), since no execution can plausibly take
    longer than that in this single-day scheduling model. Implausible values
    are *not* altered or dropped here -- Observation.is_duration_plausible
    just flags them, so:
        - stats.py's duration_mae_minutes still uses every completed
          observation (a corrupted value is still evidence that planning
          error can be very large, and hiding it would understate risk).
        - prediction.py's median-based point estimate excludes implausible
          observations from its basis (see app/productivity/prediction.py),
          so a single corrupted row cannot dominate a robust statistic that
          is meant to be trustworthy.
    This is a deliberate, documented choice, not silent data repair: nothing
    in the underlying TaskExecution/WorkSession rows is ever modified.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.execution.models import ExecutionStatus, TaskExecution
from app.execution.repository import ExecutionRepository
from app.execution.service import compute_start_delay_minutes
from app.planning.time import elapsed_minutes
from app.productivity.buckets import (
    TimeBucket,
    day_of_week_for_date,
    day_of_week_for_timestamp,
    time_bucket_for_instant,
    time_bucket_for_minutes,
)

MAX_PLAUSIBLE_DURATION_MINUTES = 1440

Period = Literal["all_time", "last_7_days"]


class Observation(BaseModel):
    """One execution, flattened with derived analysis fields.

    planned_start/planned_end/planned_date and day_of_week are a uniform
    view over legacy and canonical rows alike -- see this module's
    docstring for the exact basis each one uses.
    """

    execution_id: str
    task_name: str
    category: str
    tag: str
    priority: int
    status: ExecutionStatus

    planned_date: int | None
    planned_start: int
    planned_end: int
    planned_duration: int
    effective_planned_date: date | None = None

    created_at: str
    time_bucket: TimeBucket
    day_of_week: str

    actual_active_duration_minutes: float | None
    duration_variance_minutes: float | None
    start_delay_minutes: float | None

    focus_rating: int | None
    energy_rating: int | None
    interruption_count: int | None

    is_completed: bool
    is_skipped: bool
    is_cancelled: bool = False
    is_terminal: bool
    is_duration_plausible: bool


def build_observations(
    repository: ExecutionRepository,
    *,
    period: Period = "all_time",
    days: int | None = None,
    now: datetime | None = None,
) -> list[Observation]:
    """
    Load every execution from the repository and convert it to an Observation.

    Day-window selection: `days`, when given, explicitly overrides `period`
    and keeps only executions with created_at in [now - days, now] (`now`
    defaults to the current UTC time). When `days` is omitted (the default),
    behavior falls back to `period`: "last_7_days" uses a 7-day window,
    "all_time" (the default) keeps everything. This keeps the original
    two-value `period` API unchanged for existing callers while letting a UI
    offer arbitrary windows (7/30/90 days, ...) via `days`.
    """
    executions = repository.list_executions()

    window_days = days if days is not None else (7 if period == "last_7_days" else None)

    if window_days is not None:
        reference_time = now or datetime.now(timezone.utc)
        cutoff = reference_time - timedelta(days=window_days)
        executions = [
            execution
            for execution in executions
            if cutoff <= datetime.fromisoformat(execution.created_at) <= reference_time
        ]

    return [_to_observation(execution, repository) for execution in executions]


def _to_observation(execution: TaskExecution, repository: ExecutionRepository) -> Observation:
    start_delay = execution.start_delay_minutes
    if start_delay is None:
        start_delay = _recover_start_delay(execution, repository)

    is_completed = execution.status == ExecutionStatus.COMPLETED
    is_skipped = execution.status == ExecutionStatus.SKIPPED
    is_cancelled = execution.status == ExecutionStatus.CANCELLED

    effective_start, effective_end = _effective_planned_minutes(execution)

    if execution.canonical_planned_date is not None:
        effective_planned_date = execution.canonical_planned_date
        day_of_week = day_of_week_for_date(effective_planned_date)
        time_bucket = time_bucket_for_instant(execution.canonical_planned_start, execution.canonical_timezone)
    else:
        effective_planned_date = None
        day_of_week = day_of_week_for_timestamp(execution.created_at)
        time_bucket = time_bucket_for_minutes(effective_start)

    return Observation(
        execution_id=execution.id,
        task_name=execution.task_name,
        category=execution.category,
        tag=execution.tag,
        priority=execution.priority,
        status=execution.status,
        planned_date=execution.planned_date,
        planned_start=effective_start,
        planned_end=effective_end,
        planned_duration=execution.planned_duration,
        effective_planned_date=effective_planned_date,
        created_at=execution.created_at,
        time_bucket=time_bucket,
        day_of_week=day_of_week,
        actual_active_duration_minutes=execution.actual_active_duration_minutes,
        duration_variance_minutes=execution.duration_variance_minutes,
        start_delay_minutes=start_delay,
        focus_rating=execution.focus_rating,
        energy_rating=execution.energy_rating,
        interruption_count=execution.interruption_count,
        is_completed=is_completed,
        is_skipped=is_skipped,
        is_cancelled=is_cancelled,
        is_terminal=is_completed or is_skipped or is_cancelled,
        is_duration_plausible=_is_plausible_duration(execution.actual_active_duration_minutes),
    )


def _effective_planned_minutes(execution: TaskExecution) -> tuple[int, int]:
    """
    Return (planned_start, planned_end) as plain minutes-from-midnight,
    regardless of whether `execution` is a legacy or canonical row -- see
    this module's docstring. For a canonical row this is the *local*
    wall-clock minute-of-day in canonical_timezone, not a UTC minute.
    """
    if execution.canonical_planned_start is not None and execution.canonical_timezone is not None:
        tz = ZoneInfo(execution.canonical_timezone)
        local_start = execution.canonical_planned_start.astimezone(tz)
        local_end = (
            execution.canonical_planned_end.astimezone(tz)
            if execution.canonical_planned_end is not None
            else local_start
        )
        return local_start.hour * 60 + local_start.minute, local_end.hour * 60 + local_end.minute

    return execution.planned_start or 0, execution.planned_end or 0


def _recover_start_delay(execution: TaskExecution, repository: ExecutionRepository) -> float | None:
    """
    For executions where the status machine never computed
    start_delay_minutes (anything other than a completed execution),
    reconstruct it from the earliest work session, if one exists.

    Canonical basis: when execution.canonical_planned_start is known, the
    delay is the exact elapsed time between it and the earliest session's
    start (matching ExecutionService.complete's own canonical computation).
    Legacy fallback: otherwise, reuses app.execution.service's pure
    compute_start_delay_minutes (time-of-day comparison) exactly as before.
    """
    sessions = repository.list_sessions(execution.id)
    if not sessions:
        return None

    earliest_session = min(sessions, key=lambda session: session.started_at)

    if execution.canonical_planned_start is not None:
        earliest_start = datetime.fromisoformat(earliest_session.started_at)
        return round(elapsed_minutes(execution.canonical_planned_start, earliest_start), 2)

    if execution.planned_start is None:
        # A canonical, task-only execution (no placement, so no
        # canonical_planned_start) has no planned start time at all --
        # treating a missing legacy planned_start as midnight (0) would
        # fabricate a delay against a time that was never planned.
        return None

    return compute_start_delay_minutes(earliest_session.started_at, execution.planned_start)


def _is_plausible_duration(actual_active_duration_minutes: float | None) -> bool:
    if actual_active_duration_minutes is None:
        return True
    return 0 < actual_active_duration_minutes <= MAX_PLAUSIBLE_DURATION_MINUTES
