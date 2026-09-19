"""
data_prep.py

Turns raw TaskExecution rows (from app/execution/repository.py) into flat,
analysis-ready Observation records. This is the only module in
app/productivity/ (besides the repository itself) that touches the
execution repository -- stats.py, segments.py, prediction.py, and
insights.py all operate purely on lists of Observation and never see a
repository or a database connection.

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

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from app.execution.models import ExecutionStatus, TaskExecution
from app.execution.repository import ExecutionRepository
from app.execution.service import compute_start_delay_minutes
from app.productivity.buckets import TimeBucket, day_of_week_for_timestamp, time_bucket_for_minutes

MAX_PLAUSIBLE_DURATION_MINUTES = 1440

Period = Literal["all_time", "last_7_days"]


class Observation(BaseModel):
    """One execution, flattened with derived analysis fields."""

    execution_id: str
    task_name: str
    category: str
    tag: str
    priority: int
    status: ExecutionStatus

    planned_date: int
    planned_start: int
    planned_end: int
    planned_duration: int

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

    return Observation(
        execution_id=execution.id,
        task_name=execution.task_name,
        category=execution.category,
        tag=execution.tag,
        priority=execution.priority,
        status=execution.status,
        planned_date=execution.planned_date,
        planned_start=execution.planned_start,
        planned_end=execution.planned_end,
        planned_duration=execution.planned_duration,
        created_at=execution.created_at,
        time_bucket=time_bucket_for_minutes(execution.planned_start),
        day_of_week=day_of_week_for_timestamp(execution.created_at),
        actual_active_duration_minutes=execution.actual_active_duration_minutes,
        duration_variance_minutes=execution.duration_variance_minutes,
        start_delay_minutes=start_delay,
        focus_rating=execution.focus_rating,
        energy_rating=execution.energy_rating,
        interruption_count=execution.interruption_count,
        is_completed=is_completed,
        is_skipped=is_skipped,
        is_terminal=is_completed or is_skipped,
        is_duration_plausible=_is_plausible_duration(execution.actual_active_duration_minutes),
    )


def _recover_start_delay(execution: TaskExecution, repository: ExecutionRepository) -> float | None:
    """
    For executions where M1 never computed start_delay_minutes (anything
    other than a completed execution), reconstruct it from the earliest work
    session, if one exists. Reuses app.execution.service's pure
    compute_start_delay_minutes rather than duplicating that formula.
    """
    sessions = repository.list_sessions(execution.id)
    if not sessions:
        return None

    earliest_session = min(sessions, key=lambda session: session.started_at)
    return compute_start_delay_minutes(earliest_session.started_at, execution.planned_start)


def _is_plausible_duration(actual_active_duration_minutes: float | None) -> bool:
    if actual_active_duration_minutes is None:
        return True
    return 0 < actual_active_duration_minutes <= MAX_PLAUSIBLE_DURATION_MINUTES
