"""
models.py

Pydantic models for the execution-tracking subsystem.

This is a separate concern from app/models.py and app/planning/models.py:
those describe the scheduling *plan* (legacy Task/FixedBlock/DaySchedule,
and canonical Task/ScheduledTask/DaySchedule respectively); the models here
describe what actually happened when a planned task was worked on. A
TaskExecution stores a point-in-time snapshot of the planned task alongside
its own stable id and lifecycle status, so historical records stay accurate
even if the original task is later edited, renamed, or removed from the
schedule.

Legacy vs. canonical rows (Schedule Maxing v2 migration):
    TaskExecution now supports two kinds of rows side by side, distinguished
    by whether `task_id` is set:

    - Legacy rows (task_id is None): created via
      ExecutionService.create_execution/get_or_create_execution, exactly as
      before. planned_date/planned_start/planned_end are the app's abstract
      1-based day index and minutes-from-midnight, matching app/models.py.
      There is no recoverable real calendar date for these rows (the
      abstract day index has no anchor), so canonical_planned_date and the
      other canonical_* fields are always None here -- never guessed.

    - Canonical rows (task_id is set): created via
      ExecutionService.create_canonical_execution/get_or_create_canonical_execution
      from an app.planning.models.Task (and, when a placement exists, its
      ScheduledTask). These carry task_id, optionally scheduled_task_id and
      user_id, and a real aware planned date/instant in canonical_planned_date/
      canonical_timezone/canonical_planned_start/canonical_planned_end.
      planned_date/planned_start/planned_end are left None for these rows
      (there is no legacy day-index to store) -- planned_duration,
      task_name, category, and tag are still populated as a snapshot (see
      below), independent of task_id.

    Both kinds of rows share the same status machine, work sessions, and
    feedback fields, and both are returned by list_executions/reports --
    app/productivity/data_prep.py reads whichever planned-time/date fields
    are present and falls back to legacy behavior when the canonical ones
    are absent (see its module docstring).

Why historical name/category/tag/priority/planned_duration snapshots exist
*in addition to* task_id: task_id is a stable reference to the *current*
Task row, which may be renamed, re-categorized, re-estimated, or deleted
after this execution was created. The snapshot fields keep describing what
was actually planned and attempted at the time, so historical
analytics/exports never silently change when a task is edited later.
task_id/scheduled_task_id are additive identity, not a replacement for the
snapshot.

All legacy timestamps (created_at, updated_at, and WorkSession's
started_at/ended_at) remain timezone-aware ISO 8601 strings (see
datetime.now(timezone.utc).isoformat()), stored as TEXT in SQLite, exactly
as before -- changing that would ripple through every existing consumer
(service.py, data_prep.py, ml_features.py, buckets.py, exporters.py) for no
benefit this milestone needs. The *new* canonical timestamp/date fields
below (canonical_planned_start/end, actual_first_start_at,
actual_final_end_at, canonical_planned_date) instead use typed, timezone-
aware `datetime`/`date` values directly in the domain model, with ISO
serialization handled at the repository/export boundary (see
app/execution/repository.py's _execution_to_row/_row_to_execution) --
pydantic parses/serializes them to/from ISO 8601 text automatically.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be an aware datetime (include a UTC offset)")
    return value


class ExecutionStatus(str, Enum):
    """Lifecycle status of a TaskExecution. See service.py for the transition rules.

    completed, skipped, and cancelled are all terminal: no action is
    allowed out of any of them (see service.py's _TRANSITIONS).
    """

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.SKIPPED, ExecutionStatus.CANCELLED}
)


class WorkSession(BaseModel):
    """
    One contiguous span of active work on an execution.

    A session is opened by starting or resuming an execution and closed by
    pausing or completing it. Time outside of any session (i.e. while an
    execution is paused) is never counted as active work.
    """

    id: int | None = None
    execution_id: str
    started_at: str
    ended_at: str | None = None


class TaskExecution(BaseModel):
    """
    A single attempt at carrying out a planned task, plus its outcome.

    The planned_* fields are a snapshot taken when the execution was
    created; they do not change even if the source task is later edited.
    id is a stable unique identifier (a uuid4 string) — executions are never
    identified by task_name alone, since the same task name may be scheduled,
    executed, and re-scheduled many times. Old ids from fixtures/imported
    data may not all be valid UUID strings; `id` is intentionally typed as a
    plain str (not uuid.UUID) so such legacy identifiers are preserved
    exactly rather than rejected or rewritten.

    See the module docstring's "Legacy vs. canonical rows" section for how
    planned_date/planned_start/planned_end relate to the canonical_* fields
    below.
    """

    id: str
    task_name: str
    category: str
    tag: str

    # Legacy abstract day-index snapshot (app/models.py's day 1, day 2, ...)
    # and minutes-from-midnight. None for a canonical-only execution (no
    # legacy day-index exists for it) -- never populated by inference.
    planned_date: int | None = None
    planned_start: int | None = None
    planned_end: int | None = None
    planned_duration: int
    priority: int = Field(ge=1, le=10)

    status: ExecutionStatus
    created_at: str
    updated_at: str
    #: Local edit revision: +1 per logical mutation, work sessions included
    #: (see app/execution/service.py's "Versions" notes).
    version: int = Field(default=1, gt=0)
    #: Tombstone time (ISO 8601 UTC, like created_at/updated_at); None = live.
    #: Lifecycle `status` is unrelated: a deleted execution keeps its status.
    deleted_at: str | None = None

    # Populated only once the execution reaches "completed" (see service.py).
    actual_active_duration_minutes: float | None = None
    duration_variance_minutes: float | None = None
    start_delay_minutes: float | None = None

    # Optional user feedback, settable independently of status transitions.
    focus_rating: int | None = Field(default=None, ge=1, le=5)
    energy_rating: int | None = Field(default=None, ge=1, le=5)
    interruption_count: int | None = Field(default=None, ge=0)
    note: str | None = None

    # --- Canonical identity (Schedule Maxing v2 / Task 2) ------------------
    # Populated only for executions created through the canonical creation
    # API (ExecutionService.create_canonical_execution /
    # get_or_create_canonical_execution). None for legacy rows and for
    # migrated pre-v2 rows, which have no recoverable canonical identity.
    task_id: uuid.UUID | None = None
    scheduled_task_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    # Real planned calendar date/timezone/instants (app.planning.models
    # DaySchedule.date / ScheduledTask.timezone/planned_start/planned_end),
    # set only for canonical rows. Left unset (None) rather than guessed for
    # legacy and migrated rows -- see app/execution/db.py's migration notes.
    canonical_planned_date: date | None = None
    canonical_timezone: str | None = None
    canonical_planned_start: datetime | None = None
    canonical_planned_end: datetime | None = None

    # Actual instants, populated for legacy *and* canonical rows alike (both
    # kinds of executions have real work sessions): actual_first_start_at is
    # set once, the first time the execution is started (not reset by a
    # later resume); actual_final_end_at is set when the execution reaches
    # any terminal status (completed, skipped, or cancelled).
    actual_first_start_at: datetime | None = None
    actual_final_end_at: datetime | None = None

    @field_validator("canonical_planned_start", "canonical_planned_end", "actual_first_start_at", "actual_final_end_at")
    @classmethod
    def _instants_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return _require_aware(value, "canonical planned/actual instant")
