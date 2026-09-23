"""
app/planning/models.py

Canonical planning models for Schedule Maxing v2.

This is a *new*, parallel domain model, not a replacement for
app/models.py. app/models.py remains the documented legacy input/output
compatibility layer during migration: the existing CLI (app/main.py), the
desktop UI (app/app.py), and Greedy Optimizer v1 (app/optimizer.py) keep
using it unchanged. Canonical consumers introduced by later milestones must
build on the models here rather than growing a second, competing
representation of "a task" or "a scheduled placement".

Key differences from the legacy models:
    - Every entity has a stable UUID identity that survives serialization,
      edits, and repeated optimization (app/models.py's Task/ScheduledTask
      have no identity at all -- they are matched by name).
    - Calendar dates are real `datetime.date` values (see
      app.planning.compat for the explicit legacy day-index <-> date
      conversion), not abstract 1-based day integers.
    - A placement (ScheduledTask) stores a reference to its Task (task_id)
      plus aware UTC instants and a timezone identifier; it does not copy
      display metadata like name/category/tags. Use a TaskRegistry plus
      `project_scheduled_task_display` where display fields are needed.
    - Audit timestamps are aware UTC datetimes with a monotonically
      increasing integer `version`, not left implicit.
    - Sync-ready metadata (Milestone 3, see docs/sync-contract.md): every
      persisted entity here (Project, Task, FixedBlock, ScheduledTask) has
      an owner (`user_id`; None = a local, ownerless record), created_at/
      updated_at, a local edit revision (`version`), and a soft-deletion
      tombstone (`deleted_at`; None = live). Normal reads only ever return
      live records.

Recurrence note: RecurrenceSpec is a model only -- it describes a
recurrence *rule* attached to a template Task. Nothing here expands a
template into concrete future occurrences. When a future milestone adds
that expansion, each materialized occurrence must receive its own distinct
Task.id and reference the template it came from (e.g. a future
`recurrence_template_id` field) -- a recurring template's id must never be
treated as interchangeable with "all of its future occurrences".
"""

from __future__ import annotations

import uuid
from datetime import date as date_
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.planning.time import validate_timezone

SCHEMA_VERSION = 1


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be an aware datetime (include a UTC offset)")
    return value


def _utc_timestamp(value: datetime | None) -> datetime | None:
    """Audit timestamps (created_at/updated_at/deleted_at) are aware and normalized to UTC."""
    if value is None:
        return None
    return _require_aware(value, "timestamp").astimezone(timezone.utc)


# -----------------------------------------------------------------------------
# Recurrence
# -----------------------------------------------------------------------------


class RecurrenceFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RecurrenceSpec(BaseModel):
    """
    Model-only recurrence rule for a template Task. Does not expand
    occurrences -- see the module docstring.
    """

    frequency: RecurrenceFrequency
    interval: int = Field(default=1, gt=0, description="Repeat every N frequency units.")

    # Weekly-only: 0=Monday .. 6=Sunday (ISO weekday - 1).
    weekdays: list[int] | None = None

    # Monthly-only: 1-31. A month without that day (e.g. day 31 in April)
    # is a future occurrence-expansion concern, not a model-validity error.
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    end_date: date_ | None = None
    count: int | None = Field(default=None, gt=0)

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("weekdays, if provided, must not be empty")
        for day in value:
            if not (0 <= day <= 6):
                raise ValueError("weekday values must be between 0 (Monday) and 6 (Sunday)")
        return sorted(set(value))

    @model_validator(mode="after")
    def _validate_selectors(self) -> "RecurrenceSpec":
        if self.frequency != RecurrenceFrequency.WEEKLY and self.weekdays is not None:
            raise ValueError("weekdays is only valid for weekly recurrence")
        if self.frequency != RecurrenceFrequency.MONTHLY and self.day_of_month is not None:
            raise ValueError("day_of_month is only valid for monthly recurrence")
        if self.end_date is not None and self.count is not None:
            raise ValueError("specify at most one of end_date or count, not both")
        return self


# -----------------------------------------------------------------------------
# Time-of-day preference window
# -----------------------------------------------------------------------------


class LocalTimeWindow(BaseModel):
    """
    A preferred local time-of-day window, in minutes from local midnight.
    end_minute may be 1440 to mean "up to the following midnight".
    """

    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(ge=0, le=1440)

    @model_validator(mode="after")
    def _validate_order(self) -> "LocalTimeWindow":
        if self.end_minute <= self.start_minute:
            raise ValueError("end_minute must be after start_minute")
        return self


# -----------------------------------------------------------------------------
# Project
# -----------------------------------------------------------------------------


class Project(BaseModel):
    """Persisted with its tasks (PlanningService); no project-management service or UI."""

    id: uuid.UUID = Field(default_factory=_new_id)
    user_id: uuid.UUID | None = None
    name: str = Field(min_length=1)
    description: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _utc_timestamp(value)


# -----------------------------------------------------------------------------
# Task
# -----------------------------------------------------------------------------


class Task(BaseModel):
    """
    A canonical planning task.

    `required` means the task is mandatory within the planning request it
    belongs to (the request/document-level context that supplies that
    guarantee is out of scope for this milestone's models). `required_date`,
    when supplied, pins the task to that specific calendar date; a future
    Day Scheduler that allocates a required task to a date must schedule it
    on that date -- required_date is a hard placement constraint, not a
    preference.
    """

    id: uuid.UUID = Field(default_factory=_new_id)
    user_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)

    estimated_duration_minutes: int = Field(gt=0)
    priority: int = Field(ge=1, le=10)

    required: bool = False
    required_date: date_ | None = None
    preferred_dates: list[date_] = Field(default_factory=list)
    preferred_time_window: LocalTimeWindow | None = None

    dependency_ids: list[uuid.UUID] = Field(default_factory=list)

    deadline: datetime | None = None

    recurrence: RecurrenceSpec | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def _deadline_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return _require_aware(value, "deadline")

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_no_self_dependency(self) -> "Task":
        if self.id in self.dependency_ids:
            raise ValueError("a task cannot depend on itself")
        return self


class TaskRegistry(BaseModel):
    """Lookup of canonical Tasks by id, used by DaySchedule/DayScheduleOutput
    so placements can reference tasks without copying their display fields."""

    tasks: dict[uuid.UUID, Task] = Field(default_factory=dict)

    def add(self, task: Task) -> None:
        self.tasks[task.id] = task

    def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.tasks

    def __len__(self) -> int:
        return len(self.tasks)


# -----------------------------------------------------------------------------
# Fixed blocks and placements
# -----------------------------------------------------------------------------


class FixedBlock(BaseModel):
    """
    An immovable, already-scheduled interval (e.g. sleep, a class, a
    meeting). Distinct from ScheduledTask: a FixedBlock has no source Task
    and is never chosen by an optimizer -- it is a hard constraint the
    optimizer schedules around.
    """

    id: uuid.UUID = Field(default_factory=_new_id)
    user_id: uuid.UUID | None = None
    label: str = Field(min_length=1)
    #: What kind of commitment this is (e.g. "sleep", "food", "event"). The
    #: default mirrors app.models.FixedBlock's legacy default, so blocks
    #: created before categories existed stay valid.
    category: str = Field(default="fixed", min_length=1)

    planned_date: date_
    timezone: str
    planned_start: datetime
    planned_end: datetime

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @field_validator("planned_start", "planned_end")
    @classmethod
    def _instants_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "planned_start/planned_end")

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_order(self) -> "FixedBlock":
        if self.planned_end <= self.planned_start:
            raise ValueError("planned_end must be after planned_start")
        return self


class ScheduledTask(BaseModel):
    """
    A canonical placement of one flexible Task into a specific interval.

    Intentionally does not carry name/category/tags -- those live on the
    referenced Task (task_id) and should be read through a TaskRegistry /
    project_scheduled_task_display when a caller needs them for display.
    """

    id: uuid.UUID = Field(default_factory=_new_id)
    task_id: uuid.UUID
    #: Owner; persisted placements always carry their task's owner.
    user_id: uuid.UUID | None = None

    planned_date: date_
    timezone: str
    planned_start: datetime
    planned_end: datetime

    score: float = 0.0
    optimization_metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @field_validator("planned_start", "planned_end")
    @classmethod
    def _instants_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "planned_start/planned_end")

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_order(self) -> "ScheduledTask":
        if self.planned_end <= self.planned_start:
            raise ValueError("planned_end must be after planned_start")
        return self


class ScheduledTaskDisplay(BaseModel):
    """Read-only projection combining a placement with its task's display metadata."""

    placement: ScheduledTask
    name: str
    category: str
    tags: list[str]


def project_scheduled_task_display(
    placement: ScheduledTask,
    registry: TaskRegistry,
) -> ScheduledTaskDisplay:
    """Look up display metadata for a placement through a TaskRegistry.

    Raises KeyError if the placement's task_id is not present in the
    registry, since that indicates an inconsistent DaySchedule/Output.
    """
    task = registry.get(placement.task_id)
    if task is None:
        raise KeyError(f"task {placement.task_id} not found in registry for placement {placement.id}")
    return ScheduledTaskDisplay(
        placement=placement,
        name=task.name,
        category=task.category,
        tags=list(task.tags),
    )


# -----------------------------------------------------------------------------
# Unscheduled reporting
# -----------------------------------------------------------------------------


class UnscheduledReasonCode(str, Enum):
    NO_VALID_SLOT = "no_valid_slot"
    DEPENDENCY_CYCLE = "dependency_cycle"
    DEPENDENCY_UNRESOLVED = "dependency_unresolved"
    REQUIRED_DATE_CONFLICT = "required_date_conflict"
    WINDOW_UNSUPPORTED = "window_unsupported"
    OTHER = "other"


class UnscheduledEntry(BaseModel):
    task_id: uuid.UUID
    reason_code: UnscheduledReasonCode
    explanation: str = Field(min_length=1)


# -----------------------------------------------------------------------------
# Day-level input/output
# -----------------------------------------------------------------------------


class DaySchedule(BaseModel):
    """Canonical planning input for one local calendar day."""

    date: date_
    timezone: str
    fixed_blocks: list[FixedBlock] = Field(default_factory=list)
    task_ids: list[uuid.UUID] = Field(default_factory=list)
    tasks: TaskRegistry = Field(default_factory=TaskRegistry)

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @model_validator(mode="after")
    def _validate_task_ids_known(self) -> "DaySchedule":
        unknown = [task_id for task_id in self.task_ids if task_id not in self.tasks]
        if unknown:
            raise ValueError(f"task_ids references tasks missing from the registry: {unknown}")
        return self


def compute_total_score(placements: list[ScheduledTask]) -> float:
    return round(sum(placement.score for placement in placements), 2)


class DayScheduleOutput(BaseModel):
    """Canonical scheduling result for one local calendar day."""

    date: date_
    timezone: str
    fixed_blocks: list[FixedBlock] = Field(default_factory=list)
    tasks: TaskRegistry = Field(default_factory=TaskRegistry)
    placements: list[ScheduledTask] = Field(default_factory=list)
    unscheduled: list[UnscheduledEntry] = Field(default_factory=list)
    total_score: float = 0.0

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> "DayScheduleOutput":
        for placement in self.placements:
            if placement.task_id not in self.tasks:
                raise ValueError(f"placement {placement.id} references unknown task_id {placement.task_id}")

        for entry in self.unscheduled:
            if entry.task_id not in self.tasks:
                raise ValueError(f"unscheduled entry references unknown task_id {entry.task_id}")

        placed_ids = {placement.task_id for placement in self.placements}
        unscheduled_ids = {entry.task_id for entry in self.unscheduled}
        overlap = placed_ids & unscheduled_ids
        if overlap:
            raise ValueError(f"task(s) cannot be both scheduled and unscheduled: {sorted(str(t) for t in overlap)}")

        expected_total = compute_total_score(self.placements)
        if abs(expected_total - round(self.total_score, 2)) > 1e-6:
            raise ValueError(
                f"total_score ({self.total_score}) does not match the sum of placement "
                f"scores ({expected_total}); construct with compute_total_score(placements)"
            )

        return self


# -----------------------------------------------------------------------------
# Versioned JSON planning-document round trip
# -----------------------------------------------------------------------------


class PlanningDocument(BaseModel):
    """
    A minimal versioned envelope for round-tripping canonical planning data
    as JSON (IDs, dates, task references, and metadata), without
    introducing a database or synchronization service. Use
    `PlanningDocument.model_validate_json`/`.model_dump_json()` for the
    round trip.
    """

    schema_version: int = SCHEMA_VERSION
    day_schedule: DaySchedule
