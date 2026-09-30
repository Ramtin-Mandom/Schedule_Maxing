"""
app/execution/lifecycle.py

The execution lifecycle rules as pure functions and data -- the transition
table and the completion metrics -- with no storage dependency, so the
local ExecutionService (SQLite) and the server backend (backend/) apply
exactly the same rules. app/execution/service.py re-exports these names.

Outcomes (the Day page's Uncompleted | Tasks | Completed columns): what the
user says happened to one scheduled task is derived from its execution's
status, never stored twice:

    Tasks (pending)   no execution yet, scheduled, in_progress or paused
    Completed         completed
    Uncompleted       skipped   (the one canonical "did not happen" state;
                                 "uncompleted" is only UI wording)
    (cancelled is an attempt withdrawn by a reschedule or by the user; it is
     shown with Uncompleted but is not reopened)

Moving between columns runs lifecycle actions (outcome_actions below):
"complete" may now start from scheduled (a completion reported without
timing: no work sessions, so no duration metrics -- unknown, never 0), and
"reopen" returns a completed or skipped attempt to pending. Reopening keeps
every work session and actual_first_start_at; it only withdraws the
finishing marker (actual_final_end_at) and the completion metrics derived
from it, which the next "complete" recomputes. An attempt with recorded
work reopens as paused (work was done, it is not finished), one without as
scheduled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.execution.errors import ExecutionError
from app.execution.models import ExecutionStatus, WorkSession

# Allowed status transitions, keyed by action name rather than by target
# status alone: `start` and `resume` both land on IN_PROGRESS but from
# different, non-overlapping source statuses, so the source set has to be
# tracked per action, not just "is this target reachable from the current
# status". This is the single place all seven transition rules are defined.
# completed, skipped, and cancelled are terminal for work: only "reopen"
# (completed/skipped -> pending, see the module docstring) leaves one, and
# nothing leaves cancelled.
TRANSITIONS: dict[str, tuple[frozenset[ExecutionStatus], ExecutionStatus]] = {
    "start": (frozenset({ExecutionStatus.SCHEDULED}), ExecutionStatus.IN_PROGRESS),
    "pause": (frozenset({ExecutionStatus.IN_PROGRESS}), ExecutionStatus.PAUSED),
    "resume": (frozenset({ExecutionStatus.PAUSED}), ExecutionStatus.IN_PROGRESS),
    "complete": (
        frozenset({ExecutionStatus.SCHEDULED, ExecutionStatus.IN_PROGRESS, ExecutionStatus.PAUSED}),
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
    # The listed target is the one for an attempt without work sessions; see reopen_target.
    "reopen": (
        frozenset({ExecutionStatus.COMPLETED, ExecutionStatus.SKIPPED}),
        ExecutionStatus.SCHEDULED,
    ),
}

#: The fields a reopen withdraws: the finishing marker and what "complete" derived from it.
REOPEN_CLEARED_FIELDS = (
    "actual_final_end_at", "actual_active_duration_minutes", "duration_variance_minutes", "start_delay_minutes",
)


def reopen_target(has_sessions: bool) -> ExecutionStatus:
    """Where "reopen" lands: paused when work was recorded (it is not finished), else scheduled."""
    return ExecutionStatus.PAUSED if has_sessions else ExecutionStatus.SCHEDULED


# -----------------------------------------------------------------------------
# Outcomes: the user's answer for one scheduled task (see the module docstring)
# -----------------------------------------------------------------------------


class TaskOutcome(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCOMPLETED = "uncompleted"


_OUTCOME_OF_STATUS = {
    ExecutionStatus.SCHEDULED: TaskOutcome.PENDING,
    ExecutionStatus.IN_PROGRESS: TaskOutcome.PENDING,
    ExecutionStatus.PAUSED: TaskOutcome.PENDING,
    ExecutionStatus.COMPLETED: TaskOutcome.COMPLETED,
    ExecutionStatus.SKIPPED: TaskOutcome.UNCOMPLETED,
    ExecutionStatus.CANCELLED: TaskOutcome.UNCOMPLETED,
}


def outcome_of(status: ExecutionStatus | None) -> TaskOutcome:
    """The column of a scheduled task whose execution has `status` (None: no execution yet -> pending)."""
    return TaskOutcome.PENDING if status is None else _OUTCOME_OF_STATUS[ExecutionStatus(status)]


class OutcomeChangeError(ExecutionError):
    """The requested outcome cannot be reached from the attempt's status (only a cancelled attempt)."""


@dataclass(frozen=True)
class BulkOutcomeResult:
    """What a bulk outcome change (the Week/Month "All Tasks Complete" / "No Tasks Complete") did, by placement id."""

    changed: tuple[uuid.UUID, ...] = ()
    #: Already in the requested column.
    unchanged: tuple[uuid.UUID, ...] = ()
    #: Left as they were: cancelled attempts, which are never reopened.
    skipped: tuple[uuid.UUID, ...] = ()


def outcome_actions(status: ExecutionStatus | None, target: TaskOutcome) -> tuple[str, ...]:
    """
    The lifecycle actions that move an attempt in `status` (None: none yet)
    to `target`, in order; () when it is already there. Every transition
    goes through TRANSITIONS, so no second state machine exists.
    """
    target = TaskOutcome(target)
    current = outcome_of(status)
    if current == target:
        return ()
    if status == ExecutionStatus.CANCELLED:
        raise OutcomeChangeError("A cancelled attempt stays cancelled; plan the task again instead.")
    actions: tuple[str, ...] = ("reopen",) if current != TaskOutcome.PENDING else ()
    if target == TaskOutcome.COMPLETED:
        actions += ("complete",)
    elif target == TaskOutcome.UNCOMPLETED:
        actions += ("skip",)
    return actions


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
