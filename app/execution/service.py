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
    - Canonical executions (created via create_canonical_execution /
      get_or_create_canonical_execution) carry a real aware
      canonical_planned_start instant, so their start delay is instead the
      exact elapsed time between canonical_planned_start and
      actual_first_start_at (see complete() below) -- no time-of-day
      approximation is needed for them.

Legacy vs. canonical creation:
    create_execution/get_or_create_execution (unchanged) remain the
    supported creation path for legacy, name/day-index-snapshot executions,
    used by the still-legacy desktop UI until Task 6's UI migration.
    create_canonical_execution/get_or_create_canonical_execution are the new
    identity-aware creation path, taking an app.planning.models.Task (and
    optionally the ScheduledTask placement it came from) instead of loose
    keyword snapshot fields. Both paths produce ordinary TaskExecution rows
    that share the same status machine, work sessions, and metrics
    computation below. Since schema v3 the Task (and placement, if given)
    must already be persisted (app.planning.repository / PlanningService):
    the database rejects a link to an unknown task/placement, or to a
    placement of a different task, with ExecutionLinkError. Rows written
    before v3 keep whatever ids they had -- see app/execution/db.py.

Atomicity: every public mutation below is one logical operation (e.g.
start = status transition + new work session + first-start timestamp) and
runs inside a single ExecutionRepository.transaction() via @_atomic, so a
failure at any step rolls back every earlier step of that operation. The
repository's own per-call transactions nest inside it as savepoints and
never commit the enclosing operation early.

Versions (Milestone 3) -- the logical-mutation increment rule:
    An execution's `version` advances by exactly 1 per *logical* mutation
    -- start, pause, resume, complete, skip, cancel, record_feedback, and
    delete_execution -- however many rows that mutation writes. Work
    sessions belong to the execution aggregate: opening or closing one is
    part of the mutation that does it, so it is covered by (and bumps) the
    execution's version; a session never has a version of its own. Creation
    stores version 1. The final write of every mutation is a
    compare-and-update against the version the mutation started from, so
    two interleaved mutations can never both succeed on the same version.

Preconditions: every mutation accepts expected_version, the version the
caller last read; if the stored execution is at another version (or was
deleted), ExecutionVersionConflictError is raised and nothing -- status,
sessions, feedback -- changes. record_feedback and delete_execution
*require* it, because they overwrite/remove user data. For the state
transitions it is optional: the transition table itself is checked against
the stored status inside the same transaction, so a stale transition can
never apply to a state it was not valid for; callers that display an
execution (the desktop Execute tab) still pass the version they show.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.execution.db import get_connection
from app.execution.errors import ExecutionVersionConflictError, InvalidFeedbackError, InvalidTransitionError
from app.execution.lifecycle import (  # noqa: F401 - re-exported for existing callers
    TRANSITIONS as _TRANSITIONS,
    compute_active_duration_minutes,
    compute_start_delay_minutes,
)
from app.execution.models import ExecutionStatus, TaskExecution, WorkSession
from app.execution.repository import ExecutionRepository
from app.models import ScheduledTask
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.planning.time import elapsed_minutes

Clock = Callable[[], datetime]

def _atomic(method):
    """Run an ExecutionService method as one repository transaction (see the module docstring)."""

    @functools.wraps(method)
    def wrapper(self: "ExecutionService", *args, **kwargs):
        with self._repository.transaction():
            return method(self, *args, **kwargs)

    return wrapper


class ExecutionService:
    """Domain service for creating and driving TaskExecution records through their lifecycle."""

    def __init__(self, repository: ExecutionRepository, clock: Clock = lambda: datetime.now(timezone.utc)) -> None:
        self._repository = repository
        self._clock = clock

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    @_atomic
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

    @_atomic
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

    @_atomic
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
    # Canonical creation (Task 2 / Schedule Maxing v2 identity migration)
    # ------------------------------------------------------------------

    @_atomic
    def create_canonical_execution(
        self,
        task: CanonicalTask,
        scheduled_task: CanonicalScheduledTask | None = None,
        *,
        user_id: uuid.UUID | None = None,
    ) -> TaskExecution:
        """
        Create a new execution from a canonical Task (app.planning.models.Task)
        and, optionally, the ScheduledTask placement it came from.

        Always inserts a new row -- it never looks for an existing execution
        to reuse. Task-only executions (scheduled_task=None) are
        intentionally never deduplicated by task_id alone, since the same
        task may legitimately be attempted more than once without a
        placement; use get_or_create_canonical_execution when a placement
        *is* available and "re-selecting the same placement reuses its
        execution" is the desired behavior.

        task_name/category/tag/priority/planned_duration are still
        populated here as a historical snapshot of the Task's *current*
        values at creation time -- see the module and
        app/execution/models.py docstrings for why that snapshot exists
        alongside task_id.
        """
        return self._repository.create_execution(
            self._build_canonical_execution(task, scheduled_task, user_id=user_id)
        )

    @_atomic
    def get_or_create_canonical_execution(
        self,
        task: CanonicalTask,
        scheduled_task: CanonicalScheduledTask,
        *,
        user_id: uuid.UUID | None = None,
    ) -> TaskExecution:
        """
        Return the existing execution for this exact placement
        (scheduled_task.id), or create one.

        A repeated request for the same placement always reuses its
        execution and never overwrites its original planned snapshot. A
        *different* placement -- even one for the same task_id, e.g. after
        the task was moved to a new time -- gets its own new execution,
        since scheduled_task.id differs; the old execution's planned
        snapshot is left untouched. Two distinct tasks that happen to share
        identical labels/times remain distinct, since they always have
        distinct scheduled_task ids.
        """
        candidate = self._build_canonical_execution(task, scheduled_task, user_id=user_id)
        return self._repository.get_or_create_by_scheduled_task_id(candidate)

    def _build_canonical_execution(
        self,
        task: CanonicalTask,
        scheduled_task: CanonicalScheduledTask | None,
        *,
        user_id: uuid.UUID | None,
    ) -> TaskExecution:
        now = self._clock()
        now_iso = now.isoformat()
        tag = task.tags[0] if task.tags else ""

        if scheduled_task is not None:
            planned_duration_minutes = round(
                (scheduled_task.planned_end - scheduled_task.planned_start).total_seconds() / 60
            )
            canonical_kwargs = dict(
                scheduled_task_id=scheduled_task.id,
                canonical_planned_date=scheduled_task.planned_date,
                canonical_timezone=scheduled_task.timezone,
                canonical_planned_start=scheduled_task.planned_start,
                canonical_planned_end=scheduled_task.planned_end,
            )
        else:
            planned_duration_minutes = task.estimated_duration_minutes
            canonical_kwargs = dict(
                scheduled_task_id=None,
                canonical_planned_date=None,
                canonical_timezone=None,
                canonical_planned_start=None,
                canonical_planned_end=None,
            )

        return TaskExecution(
            id=str(uuid.uuid4()),
            task_name=task.name,
            category=task.category,
            tag=tag,
            planned_date=None,
            planned_start=None,
            planned_end=None,
            planned_duration=planned_duration_minutes,
            priority=task.priority,
            status=ExecutionStatus.SCHEDULED,
            created_at=now_iso,
            updated_at=now_iso,
            task_id=task.id,
            user_id=user_id,
            **canonical_kwargs,
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    @_atomic
    def start(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """
        scheduled -> in_progress. Opens a new work session.

        Also records actual_first_start_at the first time an execution is
        started (left unchanged by a later resume, and populated for legacy
        rows too, not just canonical ones -- "when did work actually
        begin" is meaningful regardless of which creation path produced the
        row).
        """
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "start")
        self._repository.create_session(execution_id, self._now_iso())

        if execution.actual_first_start_at is None:
            execution = execution.model_copy(update={"actual_first_start_at": self._clock()})

        return self._commit(original, execution)

    @_atomic
    def pause(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """in_progress -> paused. Closes the currently-open work session."""
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "pause")
        self._close_open_session(execution_id)
        return self._commit(original, execution)

    @_atomic
    def resume(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """paused -> in_progress. Opens a new work session."""
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "resume")
        self._repository.create_session(execution_id, self._now_iso())
        return self._commit(original, execution)

    @_atomic
    def complete(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """
        (in_progress | paused) -> completed.

        Closes any open work session, then computes actual_active_duration_minutes,
        duration_variance_minutes, and start_delay_minutes from all sessions,
        and records actual_final_end_at.

        start_delay_minutes: for a canonical execution (canonical_planned_start
        and actual_first_start_at both set), this is the exact elapsed time
        between those two aware UTC instants -- no approximation needed. For
        a legacy execution (no real planned instant), this falls back to the
        original time-of-day comparison via compute_start_delay_minutes,
        exactly as before.
        """
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "complete")
        self._close_open_session(execution_id)

        sessions = self._repository.list_sessions(execution_id)
        active_duration = compute_active_duration_minutes(sessions)
        variance = round(active_duration - execution.planned_duration, 2)

        if execution.canonical_planned_start is not None and execution.actual_first_start_at is not None:
            start_delay = round(
                elapsed_minutes(execution.canonical_planned_start, execution.actual_first_start_at), 2
            )
        elif execution.planned_start is not None and sessions:
            # Legacy fallback: only meaningful when a real legacy
            # minutes-from-midnight planned_start exists. A canonical
            # task-only execution (no placement, so no
            # canonical_planned_start) has neither -- treating a missing
            # planned_start as midnight (0) would fabricate a delay against
            # a planned time that was never set, so start_delay stays None.
            start_delay = compute_start_delay_minutes(sessions[0].started_at, execution.planned_start)
        else:
            start_delay = None

        execution = execution.model_copy(
            update={
                "actual_active_duration_minutes": active_duration,
                "duration_variance_minutes": variance,
                "start_delay_minutes": start_delay,
                "actual_final_end_at": self._clock(),
            }
        )
        return self._commit(original, execution)

    @_atomic
    def skip(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """(scheduled | in_progress | paused) -> skipped. Closes any open session; no completion metrics are computed."""
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "skip")
        self._close_open_session(execution_id)
        execution = execution.model_copy(update={"actual_final_end_at": self._clock()})
        return self._commit(original, execution)

    @_atomic
    def cancel(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        """
        (scheduled | in_progress | paused) -> cancelled. Closes any open
        session; no completion metrics are computed, mirroring skip().
        cancelled is terminal: no action is allowed out of it, same as
        completed/skipped (see _TRANSITIONS).
        """
        original = self._load(execution_id, expected_version)
        execution = self._transition(original, "cancel")
        self._close_open_session(execution_id)
        execution = execution.model_copy(update={"actual_final_end_at": self._clock()})
        return self._commit(original, execution)

    @_atomic
    def delete_execution(self, execution_id: str, *, expected_version: int) -> None:
        """
        Delete one execution as a tombstone (version + 1): it disappears
        from every normal read and from analytics, while its row and work
        sessions stay for history/synchronization. Its placement cannot get
        a new execution afterwards (get_or_create raises
        ExecutionDeletedError). Use reset_all_history for a full local purge.
        """
        self._load(execution_id, expected_version)
        self._repository.soft_delete_execution(execution_id, expected_version=expected_version, deleted_at=self._clock())

    def wire_id(self, execution_id: str) -> uuid.UUID:
        """The execution's stable wire identity (see ExecutionRepository.wire_id)."""
        return self._repository.wire_id(execution_id)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    @_atomic
    def record_feedback(
        self,
        execution_id: str,
        *,
        expected_version: int,
        focus_rating: int | None = None,
        energy_rating: int | None = None,
        interruption_count: int | None = None,
        note: str | None = None,
    ) -> TaskExecution:
        """
        Record optional user feedback. Only the provided fields are changed;
        omitted (None) arguments leave the existing stored value untouched.

        Allowed regardless of the execution's current status. Raises
        InvalidFeedbackError if a rating or interruption count is out of range,
        and ExecutionVersionConflictError if the execution is no longer at
        expected_version (so newer feedback is never overwritten).
        """
        if focus_rating is not None and not 1 <= focus_rating <= 5:
            raise InvalidFeedbackError(f"focus_rating must be between 1 and 5, got {focus_rating}.")
        if energy_rating is not None and not 1 <= energy_rating <= 5:
            raise InvalidFeedbackError(f"energy_rating must be between 1 and 5, got {energy_rating}.")
        if interruption_count is not None and interruption_count < 0:
            raise InvalidFeedbackError(
                f"interruption_count must be zero or greater, got {interruption_count}."
            )

        original = self._load(execution_id, expected_version)
        updates: dict[str, object] = {}
        if focus_rating is not None:
            updates["focus_rating"] = focus_rating
        if energy_rating is not None:
            updates["energy_rating"] = energy_rating
        if interruption_count is not None:
            updates["interruption_count"] = interruption_count
        if note is not None:
            updates["note"] = note

        return self._commit(original, original.model_copy(update=updates))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_execution(self, execution_id: str) -> TaskExecution:
        return self._repository.get_execution(execution_id)

    def find_execution_for_placement(self, scheduled_task_id: uuid.UUID) -> TaskExecution | None:
        """The existing execution for a canonical placement, or None -- never creates one."""
        return self._repository.find_by_scheduled_task_id(str(scheduled_task_id))

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        return self._repository.list_executions(status)

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        """Expose an execution's raw work sessions, e.g. for a caller computing a live elapsed-time display."""
        return self._repository.list_sessions(execution_id)

    @_atomic
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

    def _load(self, execution_id: str, expected_version: int | None) -> TaskExecution:
        """The live execution a mutation starts from, checked against the caller's precondition."""
        execution = self._repository.get_execution(execution_id)
        if expected_version is not None and execution.version != expected_version:
            raise ExecutionVersionConflictError(
                execution_id, expected_version=expected_version, current_version=execution.version
            )
        return execution

    def _transition(self, execution: TaskExecution, action: str) -> TaskExecution:
        """The execution with `action`'s status change applied (not yet written)."""
        allowed_sources, target_status = _TRANSITIONS[action]
        if execution.status not in allowed_sources:
            raise InvalidTransitionError(execution.id, execution.status, target_status)
        return execution.model_copy(update={"status": target_status})

    def _commit(self, original: TaskExecution, updated: TaskExecution) -> TaskExecution:
        """Write one logical mutation: version + 1 and updated_at, compared against the starting version."""
        updated = updated.model_copy(update={"version": original.version + 1, "updated_at": self._now_iso()})
        return self._repository.update_execution(updated, expected_version=original.version)

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
