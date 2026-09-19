"""
models.py

Pydantic models for the execution-tracking subsystem.

This is a separate concern from app/models.py: app/models.py describes the
scheduling *plan* (Task, FixedBlock, DaySchedule, ...); the models here
describe what actually happened when a planned task was worked on. A
TaskExecution stores a point-in-time snapshot of the planned task alongside
its own stable id and lifecycle status, so historical records stay accurate
even if the original task is later edited, renamed, or removed from the
schedule.

All timestamps are timezone-aware ISO 8601 strings (see
datetime.now(timezone.utc).isoformat()), stored as TEXT in SQLite.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    """Lifecycle status of a TaskExecution. See service.py for the transition rules."""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    SKIPPED = "skipped"


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
    executed, and re-scheduled many times.
    """

    id: str
    task_name: str
    category: str
    tag: str

    planned_date: int
    planned_start: int
    planned_end: int
    planned_duration: int
    priority: int = Field(ge=1, le=10)

    status: ExecutionStatus
    created_at: str
    updated_at: str

    # Populated only once the execution reaches "completed" (see service.py).
    actual_active_duration_minutes: float | None = None
    duration_variance_minutes: float | None = None
    start_delay_minutes: float | None = None

    # Optional user feedback, settable independently of status transitions.
    focus_rating: int | None = Field(default=None, ge=1, le=5)
    energy_rating: int | None = Field(default=None, ge=1, le=5)
    interruption_count: int | None = Field(default=None, ge=0)
    note: str | None = None
