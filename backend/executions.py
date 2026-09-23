"""
backend/executions.py

The execution aggregate on the server: a TaskExecution snapshot plus its
ordered work sessions (docs/sync-contract.md section 3 -- sessions have no
identity of their own outside the aggregate).

Writes are deliberately narrow so history cannot be corrupted:
    - create: the whole aggregate, validated for consistency (sessions in
      order and non-overlapping; only an in-progress execution has an open
      session, and it is the last one; a scheduled execution has no
      sessions). This is how existing local history is uploaded.
    - lifecycle actions (start/pause/resume/complete/skip/cancel): the same
      transition table and completion metrics as the desktop
      (app/execution/lifecycle.py); terminal statuses allow no action.
    - feedback: ratings, interruption count, note.
    - delete: a tombstone; sessions stay.
There is no generic update: the planned snapshot, the task/placement
references, and past sessions are immutable once stored.

task_id/scheduled_task_id: a normal execution must reference the caller's
own live task (and placement of that task). An execution uploaded from
history whose parents were never persisted or are gone sets
historical_reference=true; its ids are then stored as historical identity
without being resolved -- no placeholder parent is ever created.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.models import ExecutionStatus
from backend import models
from backend.resources import BaseVersion, RecordMeta, Strict


class SessionIn(Strict):
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None


class ExecutionFields(Strict):
    legacy_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: uuid.UUID | None = None
    scheduled_task_id: uuid.UUID | None = None
    historical_reference: bool = False
    task_name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    tag: str = Field(default="", max_length=100)
    planned_date: int | None = None
    planned_start: int | None = None
    planned_end: int | None = None
    planned_duration: int = Field(ge=0)
    priority: int = Field(ge=1, le=10)
    status: ExecutionStatus = ExecutionStatus.SCHEDULED
    sessions: list[SessionIn] = Field(default_factory=list)
    actual_active_duration_minutes: float | None = None
    duration_variance_minutes: float | None = None
    start_delay_minutes: float | None = None
    focus_rating: int | None = Field(default=None, ge=1, le=5)
    energy_rating: int | None = Field(default=None, ge=1, le=5)
    interruption_count: int | None = Field(default=None, ge=0)
    note: str | None = None
    canonical_planned_date: date | None = None
    canonical_timezone: str | None = None
    canonical_planned_start: AwareDatetime | None = None
    canonical_planned_end: AwareDatetime | None = None
    actual_first_start_at: AwareDatetime | None = None
    actual_final_end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _aggregate_rules(self):
        if self.scheduled_task_id is not None and self.task_id is None:
            raise ValueError("scheduled_task_id requires task_id")
        if self.legacy_id is not None:
            try:
                uuid.UUID(self.legacy_id)
            except ValueError:
                pass
            else:
                raise ValueError("legacy_id is only for non-UUID local ids; a UUID id is the execution's id itself")
        previous_end = None
        for index, work in enumerate(self.sessions):
            if work.ended_at is not None and work.ended_at < work.started_at:
                raise ValueError("a session cannot end before it starts")
            if previous_end is None and index > 0:
                raise ValueError("only the last session may still be open")
            if previous_end is not None and work.started_at < previous_end:
                raise ValueError("sessions must be in order and must not overlap")
            previous_end = work.ended_at
        has_open = bool(self.sessions) and self.sessions[-1].ended_at is None
        if has_open != (self.status == ExecutionStatus.IN_PROGRESS):
            raise ValueError("exactly the in-progress status has an open (last) session")
        if self.status == ExecutionStatus.SCHEDULED and self.sessions:
            raise ValueError("a scheduled execution has no sessions")
        return self


class ExecutionCreate(ExecutionFields):
    id: uuid.UUID | None = None


class SessionOut(BaseModel):
    started_at: datetime
    ended_at: datetime | None = None


class ExecutionOut(ExecutionFields, RecordMeta):
    sessions: list[SessionOut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aggregate_rules(self):  # stored aggregates are already validated
        return self


class ActionIn(Strict, BaseVersion):
    #: When the action happened (e.g. recorded offline); defaults to the server time. Not in the future.
    at: AwareDatetime | None = None


class FeedbackIn(Strict, BaseVersion):
    focus_rating: int | None = Field(default=None, ge=1, le=5)
    energy_rating: int | None = Field(default=None, ge=1, le=5)
    interruption_count: int | None = Field(default=None, ge=0)
    note: str | None = None


ACTIONS = ("start", "pause", "resume", "complete", "skip", "cancel")


class _ExecutionSpec:
    path = "executions"
    entity_type = "execution"
    label = "execution"
    model = models.Execution
    out_schema = ExecutionOut

    @staticmethod
    def sessions(session: Session, user_id: uuid.UUID, execution_id: uuid.UUID) -> list[models.WorkSession]:
        return list(session.scalars(
            select(models.WorkSession)
            .where(models.WorkSession.user_id == user_id, models.WorkSession.execution_id == execution_id)
            .order_by(models.WorkSession.position)
        ))

    def serialize(self, session: Session, user_id: uuid.UUID, row) -> dict:
        data = {name: getattr(row, name) for name in ExecutionFields.model_fields if name != "sessions"}
        data.update(
            id=row.id, version=row.version, created_at=row.created_at, updated_at=row.updated_at,
            deleted_at=row.deleted_at,
            sessions=[{"started_at": work.started_at, "ended_at": work.ended_at}
                      for work in self.sessions(session, user_id, row.id)],
        )
        return ExecutionOut.model_validate(data).model_dump(mode="json")


EXECUTIONS = _ExecutionSpec()
