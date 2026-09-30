"""
backend/resources.py

API schemas and per-resource rules for the user-scoped records. Content is
validated with the same canonical models the desktop uses
(app.planning.models / preferences / provenance), so a record is valid on
the server exactly when it is valid locally.

Input schemas forbid unknown fields: a client cannot send user_id, version,
created_at, updated_at or deleted_at -- those are owned by the server.
Relationship rules (docs/sync-contract.md sections 4-6), all checked inside
the caller's user scope only:
    project      delete refused (409 in_use) while live tasks belong to it
    task         project_id / dependency_ids must be live records of the user;
                 delete refused while live tasks depend on it; deleting it
                 tombstones its live placements (each logged)
    fixed block  the fixed-block invariants of app/planning/fixed_block_rules.py:
                 a positive whole-minute interval that starts on its
                 planned_date in its timezone and lies inside that date's
                 effective day window (backend/preferences.py) -- else 422
                 validation_error with a `reason` -- and no overlap with
                 another live block of the user -- else 409
                 fixed_block_overlap with the `conflicting` block. An update
                 that keeps the interval unchanged is not re-judged.
    placement    task_id must be a live task of the user; it cannot move to
                 another task once execution history references it;
                 optimization_metadata is a bounded extension object
                 (backend/record_mapping.check_optimization_metadata);
                 task_category is a snapshot taken on create (the client's,
                 else the task's category now) and never changes after;
                 removal_reason/superseded_by_id are set only when it is
                 removed (a delete, a task cascade, a reschedule), never by a
                 create or update
    preference   one live layer per scope ("user" or one date); scope is fixed
    generation   one live record per date; the date is fixed
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.planning import fixed_block_rules
from app.planning.models import FixedBlock as CanonicalFixedBlock
from app.planning.models import DEFAULT_TASK_POINTS, MAX_TASK_POINTS, LocalTimeWindow, RecurrenceSpec
from app.planning.models import PlacementRemovalReason
from app.planning.models import ScheduledTask as CanonicalPlacement
from app.planning.models import Task as CanonicalTask
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.provenance import GenerationRecord
from backend import models
from backend.errors import ApiError, invalid_reference
from backend.preferences import effective_day_preferences
from backend.record_mapping import (
    check_optimization_metadata,
    preference_overrides,
    task_content,
    write_preference_overrides,
    write_task,
)

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordMeta(BaseModel):
    id: uuid.UUID
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class BaseVersion(BaseModel):
    #: The server version this change was based on (the precondition).
    base_version: int = Field(gt=0)


def _validated(build: Callable[[], object]) -> None:
    try:
        build()
    except ValueError as error:  # pydantic ValidationError is a ValueError
        raise ValueError(_first_message(error)) from None


def _first_message(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors) and errors():
        return str(errors()[0].get("msg", error))
    return str(error)


class ProjectFields(Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectCreate(ProjectFields):
    id: uuid.UUID | None = None


class ProjectUpdate(ProjectFields, BaseVersion):
    pass


class ProjectOut(ProjectFields, RecordMeta):
    pass


class TaskFields(Strict):
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    tags: list[str] = Field(default_factory=list)
    estimated_duration_minutes: int = Field(gt=0)
    priority: int = Field(ge=1, le=10)
    #: The user's productivity value (not the optimizer's placement score); omitted by older clients: the default.
    points: int = Field(default=DEFAULT_TASK_POINTS, ge=0, le=MAX_TASK_POINTS)
    required: bool = False
    required_date: date_ | None = None
    preferred_dates: list[date_] = Field(default_factory=list)
    preferred_time_window: LocalTimeWindow | None = None
    dependency_ids: list[uuid.UUID] = Field(default_factory=list)
    deadline: AwareDatetime | None = None
    recurrence: RecurrenceSpec | None = None

    def canonical(self, task_id: uuid.UUID) -> CanonicalTask:
        return CanonicalTask(id=task_id, **self.model_dump(include=set(TaskFields.model_fields)))

    @model_validator(mode="after")
    def _canonical_rules(self):
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("dependency_ids must not repeat a task")
        _validated(lambda: self.canonical(getattr(self, "id", None) or uuid.uuid4()))
        return self


class TaskCreate(TaskFields):
    id: uuid.UUID | None = None


class TaskUpdate(TaskFields, BaseVersion):
    pass


class TaskOut(TaskFields, RecordMeta):
    pass


class FixedBlockFields(Strict):
    label: str = Field(min_length=1, max_length=500)
    category: str = Field(default="fixed", min_length=1, max_length=100)
    planned_date: date_
    timezone: str
    planned_start: AwareDatetime
    planned_end: AwareDatetime

    @model_validator(mode="after")
    def _canonical_rules(self):
        _validated(lambda: CanonicalFixedBlock(**self.model_dump(include=set(FixedBlockFields.model_fields))))
        return self


class FixedBlockCreate(FixedBlockFields):
    id: uuid.UUID | None = None


class FixedBlockUpdate(FixedBlockFields, BaseVersion):
    pass


class FixedBlockOut(FixedBlockFields, RecordMeta):
    pass


class PlacementFields(Strict):
    task_id: uuid.UUID
    planned_date: date_
    timezone: str
    planned_start: AwareDatetime
    planned_end: AwareDatetime
    score: float = 0.0
    optimization_metadata: dict[str, Any] = Field(default_factory=dict)
    #: The task's category when the placement was saved (docs/execution-rescheduling.md).
    task_category: str | None = Field(default=None, min_length=1, max_length=100)
    #: Set on a tombstone only: why it was removed, and the placement that replaced it.
    removal_reason: PlacementRemovalReason | None = None
    superseded_by_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _canonical_rules(self):
        _validated(lambda: CanonicalPlacement(**self.model_dump(include=set(PlacementFields.model_fields))))
        check_optimization_metadata(self.optimization_metadata)
        return self


class PlacementCreate(PlacementFields):
    id: uuid.UUID | None = None


class PlacementUpdate(PlacementFields, BaseVersion):
    pass


class PlacementOut(PlacementFields, RecordMeta):
    pass


class PreferenceFields(Strict):
    scope: Literal["user", "date"]
    date: date_ | None = None
    #: A PreferenceOverrides layer; absent keys, values, and explicit nulls keep their meaning.
    overrides: PreferenceOverrides = Field(default_factory=PreferenceOverrides)

    @model_validator(mode="after")
    def _scope_rules(self):
        if (self.scope == "date") != (self.date is not None):
            raise ValueError("a 'date' layer needs a date; a 'user' layer must not have one")
        return self


class PreferenceCreate(PreferenceFields):
    id: uuid.UUID | None = None


class PreferenceUpdate(PreferenceFields, BaseVersion):
    pass


class PreferenceOut(PreferenceFields, RecordMeta):
    pass


_GENERATION_FIELDS = (
    "planned_date", "timezone", "engine_mode", "range_start", "range_end", "range_scope", "allocation_id",
    "fingerprint", "fingerprint_version", "placements_digest", "placement_count", "unscheduled_count",
    "total_score", "generated_at",
)


class GenerationFields(Strict):
    planned_date: date_
    timezone: str
    engine_mode: OptimizerMode
    range_start: date_
    range_end: date_
    range_scope: str = Field(min_length=1, max_length=20)
    allocation_id: uuid.UUID
    fingerprint: str = Field(min_length=1, max_length=64)
    fingerprint_version: int = Field(gt=0)
    placements_digest: str = Field(min_length=1, max_length=64)
    placement_count: int = Field(ge=0)
    unscheduled_count: int = Field(ge=0)
    total_score: float
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _canonical_rules(self):
        if not self.range_start <= self.planned_date <= self.range_end:
            raise ValueError("planned_date must lie within [range_start, range_end]")
        _validated(lambda: GenerationRecord(**self.model_dump(include=set(_GENERATION_FIELDS))))
        return self


class GenerationCreate(GenerationFields):
    id: uuid.UUID | None = None


class GenerationUpdate(GenerationFields, BaseVersion):
    pass


class GenerationOut(GenerationFields, RecordMeta):
    pass


# -----------------------------------------------------------------------------
# Resource specifications (plain CRUD resources)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceSpec:
    path: str
    entity_type: str
    label: str
    model: type
    create_schema: type[BaseModel]
    update_schema: type[BaseModel]
    out_schema: type[BaseModel]
    #: (session, user_id, row) -> the record's content fields (python values). Reads only the row and
    #: its child rows, so it serves a live row and a revision row alike (backend/snapshots.py).
    content: Callable[[Session, uuid.UUID, Any], dict]
    #: (session, user_id, row, payload) -> None: write the payload's content into the row (and child rows);
    #: also used, without a session, to fill a revision row from an Out record.
    assign: Callable[[Session, uuid.UUID, Any, BaseModel], None]
    #: (session, user_id, payload, existing row or None) -> None: reference/policy checks; raise ApiError
    validate: Callable[[Session, uuid.UUID, BaseModel, Any], None] = lambda *args: None
    #: (mutator, row) -> None: relationship policy/cascades run before the row is tombstoned
    before_delete: Callable[[Any, Any], None] = lambda *args: None

    def serialize(self, session: Session, user_id: uuid.UUID, row) -> dict:
        data = {
            "id": row.id, "version": row.version, "created_at": row.created_at,
            "updated_at": row.updated_at, "deleted_at": row.deleted_at,
            **self.content(session, user_id, row),
        }
        return self.out_schema.model_validate(data).model_dump(mode="json")


def live(session: Session, model, user_id: uuid.UUID, record_id: uuid.UUID):
    row = session.get(model, (user_id, record_id))
    return row if row is not None and row.deleted_at is None else None


def _fields(payload: BaseModel, schema: type[BaseModel]) -> dict:
    return payload.model_dump(include=set(schema.model_fields) - {"id", "base_version"})


# -- projects ---------------------------------------------------------------


def _project_content(_session, _user_id, row) -> dict:
    return {"name": row.name, "description": row.description}


def _project_assign(_session, _user_id, row, payload) -> None:
    row.name, row.description = payload.name, payload.description


def _project_before_delete(mutator, row) -> None:
    in_use = mutator.session.scalars(
        select(models.Task.id).where(
            models.Task.user_id == mutator.user_id, models.Task.project_id == row.id, models.Task.deleted_at.is_(None)
        ).limit(1)
    ).first()
    if in_use is not None:
        raise ApiError(409, "in_use", "The project still has tasks; delete or move them first.")


# -- tasks -------------------------------------------------------------------


def _task_content(_session, _user_id, row) -> dict:
    return task_content(row)


def _task_assign(_session, _user_id, row, payload: TaskFields) -> None:
    write_task(row, payload)


def _task_validate(session, user_id, payload: TaskFields, existing) -> None:
    if payload.project_id is not None and live(session, models.Project, user_id, payload.project_id) is None:
        raise invalid_reference("project_id does not name one of your projects.")
    for dependency in payload.dependency_ids:
        if existing is not None and dependency == existing.id:
            raise ApiError(422, "validation_error", "A task cannot depend on itself.")
        if live(session, models.Task, user_id, dependency) is None:
            raise invalid_reference("dependency_ids must name your own existing tasks.")


def _task_before_delete(mutator, row) -> None:
    dependent = mutator.session.execute(
        select(models.TaskDependency.task_id)
        .join(models.Task, (models.Task.user_id == models.TaskDependency.user_id)
              & (models.Task.id == models.TaskDependency.task_id))
        .where(
            models.TaskDependency.user_id == mutator.user_id, models.TaskDependency.depends_on_id == row.id,
            models.Task.deleted_at.is_(None),
        ).limit(1)
    ).first()
    if dependent is not None:
        raise ApiError(409, "in_use", "Other tasks depend on this task; remove those dependencies first.")
    for placement in mutator.session.scalars(
        select(models.Placement).where(
            models.Placement.user_id == mutator.user_id, models.Placement.task_id == row.id,
            models.Placement.deleted_at.is_(None),
        )
    ):
        placement.removal_reason = PlacementRemovalReason.TASK_DELETED.value
        mutator.tombstone(PLACEMENTS, placement)


# -- fixed blocks ---------------------------------------------------------


def _block_content(_session, _user_id, row) -> dict:
    return {name: getattr(row, name) for name in FixedBlockFields.model_fields}


def _canonical_block(row) -> CanonicalFixedBlock:
    return CanonicalFixedBlock(
        id=row.id, user_id=row.user_id, **{name: getattr(row, name) for name in FixedBlockFields.model_fields}
    )


def _block_validate(session, user_id, payload: FixedBlockFields, existing) -> None:
    """
    The fixed-block write invariants (app/planning/fixed_block_rules.py) for
    a create or an update, REST and sync alike: this runs inside the
    Mutator, after the user's change-log lock is taken, so two concurrent
    writes of the same user cannot both pass the overlap check. An update
    that keeps the interval unchanged is not re-judged (historical blocks).
    """
    block = CanonicalFixedBlock(
        id=existing.id if existing is not None else getattr(payload, "id", None) or uuid.uuid4(),
        user_id=user_id, **payload.model_dump(include=set(FixedBlockFields.model_fields)),
    )
    if existing is not None and not fixed_block_rules.interval_changed(_canonical_block(existing), block):
        return
    preferences = effective_day_preferences(session, user_id, [block.planned_date], block.timezone)[block.planned_date]
    try:
        fixed_block_rules.check_interval(block, preferences)
    except fixed_block_rules.FixedBlockRuleViolation as error:
        raise ApiError(422, "validation_error", str(error), reason=error.code) from None
    first, last = fixed_block_rules.neighborhood(block.planned_date)
    nearby = session.scalars(
        select(models.FixedBlock).where(
            models.FixedBlock.user_id == user_id, models.FixedBlock.deleted_at.is_(None),
            models.FixedBlock.planned_date >= first, models.FixedBlock.planned_date <= last,
        )
    )
    other = fixed_block_rules.find_overlap(block, [_canonical_block(row) for row in nearby])
    if other is not None:
        raise ApiError(
            409, "fixed_block_overlap",
            f"The fixed block overlaps your fixed block {other.label!r} on {other.planned_date}.",
            conflicting=FIXED_BLOCKS.serialize(session, user_id, session.get(models.FixedBlock, (user_id, other.id))),
        )


def _generic_assign(schema: type[BaseModel]):
    def assign(_session, _user_id, row, payload) -> None:
        for name, value in _fields(payload, schema).items():
            setattr(row, name, value)

    return assign


# -- placements -------------------------------------------------------------


def _placement_content(_session, _user_id, row) -> dict:
    return {name: getattr(row, name) for name in PlacementFields.model_fields}


def _placement_assign(session, user_id, row, payload: PlacementFields) -> None:
    """
    The payload's content. task_category is a snapshot: kept when the payload
    has none, and taken from the task's current category when a live
    placement is created without one (a revision row stores exactly what it
    is given: it is filled without a session).
    """
    for name, value in _fields(payload, PlacementFields).items():
        if name == "task_category":
            continue
        setattr(row, name, value.value if isinstance(value, PlacementRemovalReason) else value)
    if payload.task_category is not None:
        row.task_category = payload.task_category
    elif session is not None and inspect(row).pending and row.deleted_at is None:
        task = session.get(models.Task, (user_id, payload.task_id))
        row.task_category = task.category if task is not None else None


def _placement_validate(session, user_id, payload: PlacementFields, existing) -> None:
    if payload.removal_reason is not None or payload.superseded_by_id is not None:
        raise ApiError(422, "validation_error", "removal_reason and superseded_by_id are recorded when a placement "
                                                "is removed; a create or update cannot set them.")
    if (existing is not None and existing.task_category is not None and payload.task_category is not None
            and payload.task_category != existing.task_category):
        raise ApiError(422, "validation_error", "task_category is the snapshot taken when the placement was saved; "
                                                "it cannot change.")
    if live(session, models.Task, user_id, payload.task_id) is None:
        raise invalid_reference("task_id does not name one of your tasks.")
    if existing is not None and existing.task_id != payload.task_id:
        history = session.scalars(
            select(models.Execution.id).where(
                models.Execution.user_id == user_id, models.Execution.scheduled_task_id == existing.id
            ).limit(1)
        ).first()
        if history is not None:
            raise ApiError(409, "in_use", "Execution history references this placement; it cannot move to another task.")


# -- preferences ------------------------------------------------------------


def _preference_scope_key(payload: PreferenceFields) -> str:
    return "user" if payload.scope == "user" else payload.date.isoformat()


def _preference_content(_session, _user_id, row) -> dict:
    return {"scope": row.scope, "date": row.scope_date, "overrides": preference_overrides(row)}


def _preference_assign(_session, _user_id, row, payload: PreferenceFields) -> None:
    row.scope, row.scope_date, row.scope_key = payload.scope, payload.date, _preference_scope_key(payload)
    write_preference_overrides(row, payload.overrides)


def _preference_validate(session, user_id, payload: PreferenceFields, existing) -> None:
    key = _preference_scope_key(payload)
    if existing is not None:
        if existing.scope_key != key:
            raise ApiError(422, "validation_error", "A preference layer's scope and date cannot be changed.")
        return
    current = session.scalars(
        select(models.Preference).where(
            models.Preference.user_id == user_id, models.Preference.scope_key == key,
            models.Preference.deleted_at.is_(None),
        )
    ).first()
    if current is not None:
        raise ApiError(409, "already_exists", "A preference layer for this scope already exists.",
                       current=PREFERENCES.serialize(session, user_id, current))


# -- schedule generations -----------------------------------------------------


def _generation_content(_session, _user_id, row) -> dict:
    return {name: getattr(row, name) for name in GenerationFields.model_fields}


def _generation_assign(_session, _user_id, row, payload: GenerationFields) -> None:
    for name, value in _fields(payload, GenerationFields).items():
        setattr(row, name, value.value if isinstance(value, OptimizerMode) else value)


def _generation_validate(session, user_id, payload: GenerationFields, existing) -> None:
    if existing is not None:
        if existing.planned_date != payload.planned_date:
            raise ApiError(422, "validation_error", "A schedule record's date cannot be changed.")
        return
    current = session.scalars(
        select(models.ScheduleGeneration).where(
            models.ScheduleGeneration.user_id == user_id, models.ScheduleGeneration.planned_date == payload.planned_date,
            models.ScheduleGeneration.deleted_at.is_(None),
        )
    ).first()
    if current is not None:
        raise ApiError(409, "already_exists", "A schedule record for this date already exists.",
                       current=GENERATIONS.serialize(session, user_id, current))


PROJECTS = ResourceSpec(
    "projects", "project", "project", models.Project, ProjectCreate, ProjectUpdate, ProjectOut,
    content=_project_content, assign=_project_assign, before_delete=_project_before_delete,
)
TASKS = ResourceSpec(
    "tasks", "task", "task", models.Task, TaskCreate, TaskUpdate, TaskOut,
    content=_task_content, assign=_task_assign, validate=_task_validate, before_delete=_task_before_delete,
)
FIXED_BLOCKS = ResourceSpec(
    "fixed-blocks", "fixed_block", "fixed block", models.FixedBlock, FixedBlockCreate, FixedBlockUpdate, FixedBlockOut,
    content=_block_content, assign=_generic_assign(FixedBlockFields), validate=_block_validate,
)
PLACEMENTS = ResourceSpec(
    "placements", "placement", "placement", models.Placement, PlacementCreate, PlacementUpdate, PlacementOut,
    content=_placement_content, assign=_placement_assign, validate=_placement_validate,
)
PREFERENCES = ResourceSpec(
    "preferences", "preference", "preference layer", models.Preference, PreferenceCreate, PreferenceUpdate,
    PreferenceOut, content=_preference_content, assign=_preference_assign, validate=_preference_validate,
)
GENERATIONS = ResourceSpec(
    "schedule-generations", "schedule_generation", "schedule record", models.ScheduleGeneration, GenerationCreate,
    GenerationUpdate, GenerationOut, content=_generation_content, assign=_generation_assign,
    validate=_generation_validate,
)

CRUD_RESOURCES = (PROJECTS, TASKS, FIXED_BLOCKS, PLACEMENTS, PREFERENCES, GENERATIONS)
