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

Recurrence (docs/recurrence.md): a Task whose `recurrence` is set is a
*series definition*. It is never itself scheduled. Its rule is configured
when it names an explicit local start date and IANA time zone
(RecurrenceSpec.start_date/timezone); a rule without them (every template
stored before recurrence was expanded) needs configuration and produces
nothing. app/planning/series.py materializes each original recurrence slot
as a distinct concrete Task -- an *occurrence* -- whose immutable identity is
(series_id, occurrence_slot) and whose id is derived from exactly that
(app.planning.recurrence.occurrence_task_id). An occurrence never carries a
recurrence rule of its own, so it can never be mistaken for another series;
it is planned, placed, executed, moved and deleted like any other task.
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
    The recurrence rule of a series definition (see the module docstring and
    app/planning/recurrence.py for the calendar semantics).
    """

    frequency: RecurrenceFrequency
    interval: int = Field(default=1, gt=0, description="Repeat every N frequency units.")

    # Weekly-only: 0=Monday .. 6=Sunday (ISO weekday - 1).
    weekdays: list[int] | None = None

    # Monthly-only: 1-31. A month without that day (e.g. day 31 in April)
    # has no occurrence: expansion skips it, never clamps it.
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    #: Inclusive. Before start_date it describes a retired segment with no slots.
    end_date: date_ | None = None
    count: int | None = Field(default=None, gt=0)

    #: The series anchor: its first possible local slot date. Together with `timezone`, explicit -- never
    #: derived from a requested range or a machine clock. Both None: the rule needs configuration.
    start_date: date_ | None = None
    #: The IANA time zone the slots are local dates of.
    timezone: str | None = None

    @property
    def configured(self) -> bool:
        return self.start_date is not None and self.timezone is not None

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str | None) -> str | None:
        if value is not None:
            validate_timezone(value)
        return value

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
        if (self.start_date is None) != (self.timezone is None):
            raise ValueError("a recurrence's start_date and timezone are set together (or neither: not configured)")
        return self


class OccurrenceState(str, Enum):
    """
    The exception state of one materialized occurrence (docs/recurrence.md).
    None (the usual case) means the occurrence follows its series definition.
    """

    #: Edited on its own ("this occurrence", or moved to another date): later series-wide edits leave it alone.
    MODIFIED = "modified"
    #: Skipped by the user (a tombstone that keeps its slot reserved).
    SKIPPED = "skipped"
    #: Deleted by the user (a tombstone that keeps its slot reserved).
    DELETED = "deleted"
    #: Removed by a series-wide or "this and every later occurrence" change (a tombstone keeping its slot).
    SUPERSEDED = "superseded"


#: States that only a tombstoned occurrence carries.
OCCURRENCE_TOMBSTONE_STATES = frozenset({OccurrenceState.SKIPPED, OccurrenceState.DELETED, OccurrenceState.SUPERSEDED})


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


#: Task.points: the default of a new task, and the largest value accepted.
DEFAULT_TASK_POINTS = 1
MAX_TASK_POINTS = 1000

#: ProjectMilestone.score: the score of a new milestone, and the accepted range.
MIN_MILESTONE_SCORE = 1
MAX_MILESTONE_SCORE = 10
#: The largest (and, negated, smallest) milestone number accepted.
MAX_MILESTONE_NUMBER = 1_000_000


class ProjectMilestone(BaseModel):
    """One milestone of a project: stored with it (Project.milestones), ordered by `number`."""

    id: uuid.UUID = Field(default_factory=_new_id)
    #: The user's ordering number; several milestones may share one (they keep the order they were added in).
    number: int = Field(ge=-MAX_MILESTONE_NUMBER, le=MAX_MILESTONE_NUMBER)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    score: int = Field(default=MIN_MILESTONE_SCORE, ge=MIN_MILESTONE_SCORE, le=MAX_MILESTONE_SCORE)


class ProjectTaskDefaults(BaseModel):
    """
    What a project fills in for a new task of its own. Each value is either
    explicitly configured or None (not set): an unset one never stands in for
    a category's or the application's default.
    """

    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    priority: int | None = Field(default=None, ge=1, le=10)
    points: int | None = Field(default=None, ge=0, le=MAX_TASK_POINTS)

    @property
    def configured(self) -> bool:
        return any(value is not None for value in (self.duration_minutes, self.priority, self.points))


class Project(BaseModel):
    """Persisted with its tasks (PlanningService), its planned dates, completion and milestones."""

    id: uuid.UUID = Field(default_factory=_new_id)
    user_id: uuid.UUID | None = None
    name: str = Field(min_length=1)
    description: str | None = None
    #: The planned span (both optional; a record stored before they existed has neither).
    start_date: date_ | None = None
    estimated_end_date: date_ | None = None
    #: When the project was marked complete (None: ongoing).
    completed_at: datetime | None = None
    #: In the order they were added; shown by number (ordered_milestones).
    milestones: list[ProjectMilestone] = Field(default_factory=list)
    #: The defaults of this project's new tasks (a new project configures none).
    task_defaults: ProjectTaskDefaults = Field(default_factory=ProjectTaskDefaults)

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("created_at", "updated_at", "deleted_at", "completed_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_details(self) -> "Project":
        if (self.start_date is not None and self.estimated_end_date is not None
                and self.estimated_end_date < self.start_date):
            raise ValueError("estimated_end_date must not be before start_date")
        ids = [milestone.id for milestone in self.milestones]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate milestone ids")
        return self

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    @property
    def ordered_milestones(self) -> list[ProjectMilestone]:
        """Ascending by number; equal numbers keep the order they were added in (a stable sort)."""
        return sorted(self.milestones, key=lambda milestone: milestone.number)


#: The Project fields beyond name and description (storage and synchronization carry them together).
PROJECT_DETAIL_FIELDS = ("start_date", "estimated_end_date", "completed_at", "milestones", "task_defaults")


def project_abbreviation(project_name: str | None) -> str:
    """The first three characters of the trimmed name (all of a shorter one), capitalization kept."""
    return (project_name or "").strip()[:3]


def task_display_name(task_name: str, project_name: str | None) -> str:
    """
    A task's name as schedule views show it: with its project's abbreviation
    in parentheses ("Read ch. 3 (mat)"). Display text only -- the stored name
    is never changed -- and a name without a project is returned as it is.
    """
    abbreviation = project_abbreviation(project_name)
    return f"{task_name} ({abbreviation})" if abbreviation else task_name


# -----------------------------------------------------------------------------
# Task type
# -----------------------------------------------------------------------------


#: The namespace of derived task-type ids: uuid5(TASK_TYPE_NAMESPACE, "<root task id>").
TASK_TYPE_NAMESPACE = uuid.UUID("3b9d6c1e-52a7-4f0b-8e64-1c7a9d2f5e08")


def derived_task_type_id(root_task_id: uuid.UUID) -> uuid.UUID:
    """
    The deterministic type id of the work `root_task_id` stands for: a
    standalone task's own id, or the oldest provable root of a recurring
    series' lineage. Every device and the server derive the same id from the
    same root, and two unrelated tasks never share one (names are never
    compared).
    """
    return uuid.uuid5(TASK_TYPE_NAMESPACE, str(root_task_id))


class TaskType(BaseModel):
    """
    A reusable, owner-scoped kind of work (docs/productivity-redesign-plan.md,
    contract A): a stable identity with a display label. Independent of a
    task's category and tags, and distinct from an occurrence: every
    occurrence of a recurring series shares its series' type.
    """

    id: uuid.UUID = Field(default_factory=_new_id)
    user_id: uuid.UUID | None = None
    label: str = Field(min_length=1, max_length=500)

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
    #: The user's own productivity value of the task (the task form's "Points"): what finishing it is worth to
    #: them, for analytics. Not a scheduling input and unrelated to a placement's optimizer `score`.
    points: int = Field(default=DEFAULT_TASK_POINTS, ge=0, le=MAX_TASK_POINTS)
    #: The reusable type (TaskType) this task is an instance of; not a scheduling input. None: not assigned --
    #: PlanningService assigns one when the task is saved (an occurrence inherits its series' type, a series
    #: segment its predecessor's), so None only survives on a record that predates types and was never rewritten.
    task_type_id: uuid.UUID | None = None

    required: bool = False
    required_date: date_ | None = None
    preferred_dates: list[date_] = Field(default_factory=list)
    preferred_time_window: LocalTimeWindow | None = None

    dependency_ids: list[uuid.UUID] = Field(default_factory=list)

    deadline: datetime | None = None

    #: Set: this task is a series definition (never scheduled itself; see the module docstring).
    recurrence: RecurrenceSpec | None = None

    # -- recurrence identity (docs/recurrence.md) ---------------------------------------
    #: An occurrence: the series it was materialized from (immutable). None for every other task.
    series_id: uuid.UUID | None = None
    #: An occurrence: its original local slot date in the series' time zone (immutable, whatever date it is
    #: moved to). Together with series_id its identity: id == occurrence_task_id(series_id, occurrence_slot).
    occurrence_slot: date_ | None = None
    #: An occurrence's exception state (None: it follows its series definition).
    occurrence_state: OccurrenceState | None = None
    #: Provenance: the series definition's version this occurrence was materialized (or last refreshed) from.
    series_version: int | None = Field(default=None, gt=0)
    #: A series definition: the series segment it continues (a "this and every later occurrence" change).
    series_predecessor_id: uuid.UUID | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @property
    def is_series(self) -> bool:
        """A series definition (a recurrence rule), never scheduled itself."""
        return self.recurrence is not None

    @property
    def is_occurrence(self) -> bool:
        return self.series_id is not None

    @property
    def needs_configuration(self) -> bool:
        """A series definition without an explicit start date and time zone (e.g. stored before expansion)."""
        return self.recurrence is not None and not self.recurrence.configured

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

    @model_validator(mode="after")
    def _validate_recurrence_identity(self) -> "Task":
        if (self.series_id is None) != (self.occurrence_slot is None):
            raise ValueError("an occurrence names both its series_id and its occurrence_slot (or neither)")
        if self.series_id is None:
            if self.occurrence_state is not None or self.series_version is not None:
                raise ValueError("occurrence_state and series_version belong to occurrences only")
        else:
            from app.planning.recurrence import occurrence_task_id

            if self.recurrence is not None:
                raise ValueError("an occurrence cannot carry a recurrence rule of its own")
            if self.series_predecessor_id is not None:
                raise ValueError("series_predecessor_id belongs to series definitions only")
            if self.series_id == self.id:
                raise ValueError("an occurrence cannot be its own series")
            if self.id != occurrence_task_id(self.series_id, self.occurrence_slot):
                raise ValueError("an occurrence's id is derived from its series and slot (occurrence_task_id)")
            if self.occurrence_state in OCCURRENCE_TOMBSTONE_STATES and self.deleted_at is None:
                raise ValueError(f"occurrence_state {self.occurrence_state.value!r} is recorded on a tombstone only")
        if self.series_predecessor_id is not None and (self.recurrence is None or not self.recurrence.configured):
            raise ValueError("series_predecessor_id belongs to configured series definitions only")
        if self.series_predecessor_id is not None and self.series_predecessor_id == self.id:
            raise ValueError("a series cannot continue itself")
        if self.recurrence is not None and self.recurrence.configured:
            # The rule dates every occurrence; single-date constraints do not apply to the definition itself.
            if self.required_date is not None or self.preferred_dates or self.deadline is not None:
                raise ValueError("a configured recurring series is dated by its rule: it has no required date, "
                                 "preferred dates or deadline of its own")
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


class PlacementRemovalReason(str, Enum):
    """
    Why a placement stopped being part of the plan (set on its tombstone; see
    docs/execution-rescheduling.md). A tombstone without a reason was removed
    before reasons were recorded: its reason is unknown and never guessed.
    """

    #: An explicit reschedule moved it; superseded_by_id is the replacement.
    RESCHEDULED = "rescheduled"
    #: A schedule generation replaced or dropped it; superseded_by_id is the new placement of the same
    #: occurrence when that generation saved one, else None (the occurrence was not placed again).
    REGENERATED = "regenerated"
    #: The placement itself was deleted.
    DELETED = "deleted"
    #: Its task was deleted.
    TASK_DELETED = "task_deleted"
    #: A range reset/clear removed it.
    RESET = "reset"


class PlacementOrigin(str, Enum):
    """
    How a placement came to be (docs/execution-rescheduling.md, "Manual
    placements"). None on a placement means unknown: it was saved before
    origins were recorded, and is never guessed from its coordinates.
    """

    #: Produced by a schedule generation.
    GENERATED = "generated"
    #: Put there deliberately by the user (an explicit reschedule/move).
    MANUAL = "manual"


class ScheduledTask(BaseModel):
    """
    A canonical placement of one flexible Task into a specific interval.

    Intentionally does not carry name/category/tags -- those live on the
    referenced Task (task_id) and should be read through a TaskRegistry /
    project_scheduled_task_display when a caller needs them for display.
    The exception is the *planning snapshot* taken when the placement was
    saved (like an execution's snapshot): task_category, task_name,
    task_tags, task_points, task_estimate_minutes and the task's type
    (task_type_id, task_type_label). The plan therefore stays readable and
    comparable after the task is renamed, re-categorised, re-tagged or
    re-pointed, even when no execution was ever created. A snapshot is
    never rewritten: a move or regeneration tombstones the placement with
    its snapshot intact and the replacement takes its own. Each field is
    None when it was not recorded (a placement saved before snapshots):
    unknown, never back-filled from the current task.

    Removal provenance (docs/execution-rescheduling.md): a tombstone keeps
    its planned values and records why it was removed (removal_reason) and,
    when it was replaced for the same occurrence, by which placement
    (superseded_by_id). Both are None on a live placement.
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

    #: The task's category when the placement was saved (None: saved before it was recorded).
    task_category: str | None = Field(default=None, min_length=1)
    #: The rest of the planning snapshot (see the class docstring); each None = not recorded. An empty tag
    #: list and zero points are known values.
    task_name: str | None = Field(default=None, min_length=1)
    task_tags: list[str] | None = None
    task_points: int | None = Field(default=None, ge=0)
    task_estimate_minutes: int | None = Field(default=None, gt=0)
    task_type_id: uuid.UUID | None = None
    task_type_label: str | None = Field(default=None, min_length=1)
    removal_reason: PlacementRemovalReason | None = None
    superseded_by_id: uuid.UUID | None = None
    #: How it came to be (None: unknown, saved before origins were recorded).
    origin: PlacementOrigin | None = None
    #: Manual intent: schedule generation keeps it exactly where it is until the intent is released
    #: (PlanningService.release_manual_placement). Only a manual placement carries it.
    preserved: bool = False

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
        if self.superseded_by_id is not None and self.superseded_by_id == self.id:
            raise ValueError("a placement cannot supersede itself")
        if self.preserved and self.origin != PlacementOrigin.MANUAL:
            raise ValueError("only a manual placement can be preserved")
        return self


#: The planning snapshot of a placement: history, written once when the placement is saved.
PLACEMENT_SNAPSHOT_FIELDS = ("task_category", "task_name", "task_tags", "task_points", "task_estimate_minutes",
                             "task_type_id", "task_type_label")


def placement_snapshot(task: Task, task_type: TaskType | None = None) -> dict[str, Any]:
    """The planning snapshot of `task` as it is now (task_type: its type record, when there is one)."""
    return {
        "task_category": task.category, "task_name": task.name, "task_tags": list(task.tags),
        "task_points": task.points, "task_estimate_minutes": task.estimated_duration_minutes,
        "task_type_id": task.task_type_id,
        "task_type_label": task_type.label if task_type is not None and task_type.id == task.task_type_id else None,
    }


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
