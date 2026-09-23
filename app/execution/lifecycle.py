"""
app/execution/lifecycle.py

The execution lifecycle rules as pure functions and data -- the transition
table and the completion metrics -- with no storage dependency, so the
local ExecutionService (SQLite) and the server backend (backend/) apply
exactly the same rules. app/execution/service.py re-exports these names.
"""

from __future__ import annotations

from datetime import datetime

from app.execution.models import ExecutionStatus, WorkSession

# Allowed status transitions, keyed by action name rather than by target
# status alone: `start` and `resume` both land on IN_PROGRESS but from
# different, non-overlapping source statuses, so the source set has to be
# tracked per action, not just "is this target reachable from the current
# status". This is the single place all six transition rules are defined.
# completed, skipped, and cancelled are all terminal -- no action is listed
# as reachable from any of them, so every caller must reject
# attempts to leave a terminal status.
TRANSITIONS: dict[str, tuple[frozenset[ExecutionStatus], ExecutionStatus]] = {
    "start": (frozenset({ExecutionStatus.SCHEDULED}), ExecutionStatus.IN_PROGRESS),
    "pause": (frozenset({ExecutionStatus.IN_PROGRESS}), ExecutionStatus.PAUSED),
    "resume": (frozenset({ExecutionStatus.PAUSED}), ExecutionStatus.IN_PROGRESS),
    "complete": (
        frozenset({ExecutionStatus.IN_PROGRESS, ExecutionStatus.PAUSED}),
        ExecutionStatus.COMPLETED,
    ),
    "skip": (
        frozenset({ExecutionStatus.SCHEDULED, ExecutionStatus.IN_PROGRESS, ExecutionStatus.PAUSED}),
        ExecutionStatus.SKIPPED,
    ),
    "cancel": (
        frozenset({ExecutionStatus.SCHEDULED, ExecutionStatus.IN_PROGRESS, ExecutionStatus.PAUSED}),
        ExecutionStatus.CANCELLED,
    ),
}


def compute_active_duration_minutes(sessions: list[WorkSession]) -> float:
    """
    Sum the duration of every *closed* work session, in minutes.

    Open sessions (ended_at is None) are ignored, and gaps between sessions
    (time spent paused) are never inside any session, so they are excluded
    automatically rather than needing special-case handling.
    """
    total_minutes = 0.0

    for session in sessions:
        if session.ended_at is None:
            continue

        started_at = datetime.fromisoformat(session.started_at)
        ended_at = datetime.fromisoformat(session.ended_at)
        total_minutes += (ended_at - started_at).total_seconds() / 60

    return round(total_minutes, 2)


def compute_start_delay_minutes(first_started_at: str, planned_start: int) -> float:
    """
    Minutes between the planned start-of-day time and when work actually began.

    Positive means the task was started later than planned; negative means
    earlier. See the module docstring for why this compares time-of-day
    rather than full timestamps: this app's planned_date is an abstract day
    index, not a real calendar date, so only the time-of-day component of
    planned_start is meaningfully comparable to a real clock reading.

    The time-of-day is read directly from first_started_at's own recorded
    timezone (UTC, for timestamps produced by the default clock) rather than
    converted to the host machine's local timezone, so this calculation
    depends only on its inputs and not on where it happens to run.
    """
    started_at = datetime.fromisoformat(first_started_at)
    minutes_since_midnight = started_at.hour * 60 + started_at.minute + started_at.second / 60
    return round(minutes_since_midnight - planned_start, 2)
