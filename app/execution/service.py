"""
service.py

The execution-tracking service boundary. This is the only module the UI or
future analytics code should call into for execution tracking — it is
responsible for every business rule (valid state transitions, session
bookkeeping, completion metrics, feedback validation) so that callers never
need to execute SQL or reimplement the state machine themselves.

Time handling:
    - created_at/updated_at and work-session timestamps are timezone-aware
      ISO 8601 strings, produced by an injectable `clock` (defaults to
      datetime.now(timezone.utc)) so tests can control time deterministically
      without sleeping or editing stored rows directly.
    - Scheduling in this app uses an abstract "planned_date" (a day index,
      e.g. day 1, day 2, ...), not a real calendar date, and planned_start /
      planned_end are minutes-from-midnight. There is therefore no reliable
      way to map a planned time onto a specific real-world instant. Start
      delay is computed as a same-day time-of-day comparison: the time-of-day
      (in minutes since midnight, in whatever timezone the timestamp was
      recorded in — UTC by default) at which the first work session actually
      began, minus planned_start. This is an approximation by design,
      documented here and on compute_start_delay_minutes below.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.execution.db import get_connection
from app.execution.errors import InvalidFeedbackError, InvalidTransitionError
from app.execution.models import ExecutionStatus, TaskExecution, WorkSession
from app.execution.repository import ExecutionRepository
from app.models import ScheduledTask

Clock = Callable[[], datetime]

# Allowed status transitions, keyed by action name rather than by target
# status alone: `start` and `resume` both land on IN_PROGRESS but from
# different, non-overlapping source statuses, so the source set has to be
# tracked per action, not just "is this target reachable from the current
# status". This is the single place all five transition rules are defined.
_TRANSITIONS: dict[str, tuple[frozenset[ExecutionStatus], ExecutionStatus]] = {
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


class ExecutionService:
    """Domain service for creating and driving TaskExecution records through their lifecycle."""

    def __init__(self, repository: ExecutionRepository, clock: Clock = lambda: datetime.now(timezone.utc)) -> None:
        self._repository = repository
        self._clock = clock

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_execution(
        self,
        *,
        task_name: str,
        category: str,
        tag: str,
        planned_date: int,
        planned_start: int,
        planned_end: int,
        planned_duration: int,
        priority: int,
    ) -> TaskExecution:
        """Create a new execution in 'scheduled' status, snapshotting the given planned task fields."""
        now = self._now_iso()
        execution = TaskExecution(
            id=str(uuid.uuid4()),
            task_name=task_name,
            category=category,
            tag=tag,
            planned_date=planned_date,
            planned_start=planned_start,
            planned_end=planned_end,
            planned_duration=planned_duration,
            priority=priority,
            status=ExecutionStatus.SCHEDULED,
            created_at=now,
            updated_at=now,
        )
        return self._repository.create_execution(execution)

    def create_execution_from_scheduled_task(
        self,
        scheduled_task: ScheduledTask,
        *,
        planned_date: int,
        priority: int,
    ) -> TaskExecution:
        """
        Convenience constructor from an optimizer ScheduledTask.

        ScheduledTask does not carry priority or an abstract day index, so
        those are supplied separately by the caller; planned_duration is
        derived from the scheduled time window.
        """
        return self.create_execution(
            task_name=scheduled_task.name,
            category=scheduled_task.category,
            tag=scheduled_task.tag,
            planned_date=planned_date,
            planned_start=scheduled_task.time_window.start_time,
            planned_end=scheduled_task.time_window.end_time,
            planned_duration=scheduled_task.time_window.end_time - scheduled_task.time_window.start_time,
            priority=priority,
        )

    def get_or_create_execution(
        self,
        *,
        task_name: str,
        category: str,
        tag: str,
        planned_date: int,
        planned_start: int,
        planned_end: int,
        planned_duration: int,
        priority: int,
    ) -> TaskExecution:
        """
        Return an existing execution with this exact planned snapshot
        (matched on task_name, category, tag, planned_date, planned_start,
        planned_end, planned_duration -- regardless of its current status),
        or create a new one if none exists.

        This prevents duplicate execution records when a UI re-selects "the
        same" scheduled task after the schedule has been refreshed,
        re-optimized with identical inputs, or reopened -- the caller does
        not need to track execution ids itself across those events.

        Limitation: two genuinely distinct tasks that happen to share an
        identical planned snapshot are indistinguishable by this lookup and
        will collapse onto the same execution. That trade-off is intentional
        here: preventing accidental duplicates on refresh matters more than
        disambiguating true snapshot collisions, which this app's data model
        (task_name is not a unique id) cannot otherwise resolve.
        """
        snapshot = (task_name, category, tag, planned_date, planned_start, planned_end, planned_duration)

        for execution in self._repository.list_executions():
            existing_snapshot = (
                execution.task_name,
                execution.category,
                execution.tag,
                execution.planned_date,
                execution.planned_start,
                execution.planned_end,
                execution.planned_duration,
            )
            if existing_snapshot == snapshot:
                return execution

        return self.create_execution(
            task_name=task_name,
            category=category,
            tag=tag,
            planned_date=planned_date,
            planned_start=planned_start,
            planned_end=planned_end,
            planned_duration=planned_duration,
            priority=priority,
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def start(self, execution_id: str) -> TaskExecution:
        """scheduled -> in_progress. Opens a new work session."""
        execution = self._transition(execution_id, "start")
        self._repository.create_session(execution_id, self._now_iso())
        return execution

    def pause(self, execution_id: str) -> TaskExecution:
        """in_progress -> paused. Closes the currently-open work session."""
        execution = self._transition(execution_id, "pause")
        self._close_open_session(execution_id)
        return execution

    def resume(self, execution_id: str) -> TaskExecution:
        """paused -> in_progress. Opens a new work session."""
        execution = self._transition(execution_id, "resume")
        self._repository.create_session(execution_id, self._now_iso())
        return execution

    def complete(self, execution_id: str) -> TaskExecution:
        """
        (in_progress | paused) -> completed.

        Closes any open work session, then computes actual_active_duration_minutes,
        duration_variance_minutes, and start_delay_minutes from all sessions.
        """
        execution = self._transition(execution_id, "complete")
        self._close_open_session(execution_id)

        sessions = self._repository.list_sessions(execution_id)
        active_duration = compute_active_duration_minutes(sessions)
        variance = round(active_duration - execution.planned_duration, 2)
        start_delay = compute_start_delay_minutes(sessions[0].started_at, execution.planned_start) if sessions else None

        execution = execution.model_copy(
            update={
                "actual_active_duration_minutes": active_duration,
                "duration_variance_minutes": variance,
                "start_delay_minutes": start_delay,
                "updated_at": self._now_iso(),
            }
        )
        return self._repository.update_execution(execution)

    def skip(self, execution_id: str) -> TaskExecution:
        """(scheduled | in_progress | paused) -> skipped. Closes any open session; no metrics are computed."""
        execution = self._transition(execution_id, "skip")
        self._close_open_session(execution_id)
        return execution

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def record_feedback(
        self,
        execution_id: str,
        *,
        focus_rating: int | None = None,
        energy_rating: int | None = None,
        interruption_count: int | None = None,
        note: str | None = None,
    ) -> TaskExecution:
        """
        Record optional user feedback. Only the provided fields are changed;
        omitted (None) arguments leave the existing stored value untouched.

        Allowed regardless of the execution's current status. Raises
        InvalidFeedbackError if a rating or interruption count is out of range.
        """
        if focus_rating is not None and not 1 <= focus_rating <= 5:
            raise InvalidFeedbackError(f"focus_rating must be between 1 and 5, got {focus_rating}.")
        if energy_rating is not None and not 1 <= energy_rating <= 5:
            raise InvalidFeedbackError(f"energy_rating must be between 1 and 5, got {energy_rating}.")
        if interruption_count is not None and interruption_count < 0:
            raise InvalidFeedbackError(
                f"interruption_count must be zero or greater, got {interruption_count}."
            )

        execution = self._repository.get_execution(execution_id)
        updates: dict[str, object] = {"updated_at": self._now_iso()}
        if focus_rating is not None:
            updates["focus_rating"] = focus_rating
        if energy_rating is not None:
            updates["energy_rating"] = energy_rating
        if interruption_count is not None:
            updates["interruption_count"] = interruption_count
        if note is not None:
            updates["note"] = note

        execution = execution.model_copy(update=updates)
        return self._repository.update_execution(execution)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_execution(self, execution_id: str) -> TaskExecution:
        return self._repository.get_execution(execution_id)

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        return self._repository.list_executions(status)

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        """Expose an execution's raw work sessions, e.g. for a caller computing a live elapsed-time display."""
        return self._repository.list_sessions(execution_id)

    def reset_all_history(self) -> int:
        """
        Permanently delete all execution history (executions and their work
        sessions). Returns the number of executions deleted.

        This is a destructive operation with no confirmation of its own --
        callers (the UI) are expected to require explicit user confirmation
        before invoking it, and to keep the control for it well separated
        from normal navigation.
        """
        return self._repository.delete_all_executions()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _transition(self, execution_id: str, action: str) -> TaskExecution:
        allowed_sources, target_status = _TRANSITIONS[action]
        execution = self._repository.get_execution(execution_id)

        if execution.status not in allowed_sources:
            raise InvalidTransitionError(execution_id, execution.status, target_status)

        execution = execution.model_copy(update={"status": target_status, "updated_at": self._now_iso()})
        return self._repository.update_execution(execution)

    def _close_open_session(self, execution_id: str) -> None:
        open_session = self._repository.get_open_session(execution_id)
        if open_session is not None:
            assert open_session.id is not None
            self._repository.close_session(open_session.id, self._now_iso())

    def _now_iso(self) -> str:
        return self._clock().isoformat()


@contextmanager
def open_execution_service(db_path: str | Path | None = None) -> Iterator[ExecutionService]:
    """
    Open a connection to the execution database and yield a ready-to-use
    ExecutionService, closing the connection on exit.

    Pass an explicit db_path (e.g. a pytest tmp_path) in tests; omit it to
    use the configured default local data directory.
    """
    connection = get_connection(db_path)
    try:
        yield ExecutionService(ExecutionRepository(connection))
    finally:
        connection.close()
