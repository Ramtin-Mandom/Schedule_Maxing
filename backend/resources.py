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
    placement    task_id must be a live task of the user; it cannot move to
                 another task once execution history references it
    preference   one live layer per scope ("user" or one date); scope is fixed
    generation   one live record per date; the date is fixed
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.planning.models import FixedBlock as CanonicalFixedBlock
from app.planning.models import LocalTimeWindow, RecurrenceSpec
from app.planning.models import ScheduledTask as CanonicalPlacement
from app.planning.models import Task as CanonicalTask
from app.planning.preferences import OptimizerMode, PreferenceOverrides, overrides_to_document
from app.planning.provenance import GenerationRecord
from backend import models
from backend.errors import ApiError, invalid_reference

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

    @model_validator(mode="after")
    def _canonical_rules(self):
        _validated(lambda: CanonicalPlacement(**self.model_dump(include=set(PlacementFields.model_fields))))
        try:
            json.dumps(self.optimization_metadata, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("optimization_metadata must be a JSON object") from None
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
    #: (session, user_id, row) -> the record's content fields (python values)
    content: Callable[[Session, uuid.UUID, Any], dict]
    #: (session, user_id, row, payload) -> None: write the payload's content into the row (and child rows)
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


def _task_content(session, user_id, row) -> dict:
    dependencies = session.scalars(
        select(models.TaskDependency.depends_on_id)
        .where(models.TaskDependency.user_id == user_id, models.TaskDependency.task_id == row.id)
        .order_by(models.TaskDependency.position)
    ).all()
    window = None
    if row.preferred_window_start_minute is not None:
        window = {"start_minute": row.preferred_window_start_minute, "end_minute": row.preferred_window_end_minute}
    return {
        "project_id": row.project_id, "name": row.name, "category": row.category, "tags": list(row.tags),
        "estimated_duration_minutes": row.estimated_duration_minutes, "priority": row.priority,
        "required": row.required, "required_date": row.required_date,
        "preferred_dates": [date_.fromisoformat(value) for value in row.preferred_dates],
        "preferred_time_window": window, "dependency_ids": list(dependencies),
        "deadline": datetime.fromisoformat(row.deadline) if row.deadline else None,
        "recurrence": row.recurrence,
    }


def _task_assign(session, user_id, row, payload: TaskFields) -> None:
    window = payload.preferred_time_window
    row.project_id = payload.project_id
    row.name, row.category, row.tags = payload.name, payload.category, list(payload.tags)
    row.estimated_duration_minutes, row.priority, row.required = (
        payload.estimated_duration_minutes, payload.priority, payload.required,
    )
    row.required_date = payload.required_date
    row.preferred_dates = [day.isoformat() for day in payload.preferred_dates]
    row.preferred_window_start_minute = window.start_minute if window else None
    row.preferred_window_end_minute = window.end_minute if window else None
    row.deadline = payload.deadline.isoformat() if payload.deadline else None
    row.deadline_utc = payload.deadline if payload.deadline else None
    row.recurrence = payload.recurrence.model_dump(mode="json") if payload.recurrence else None
    session.flush()  # the task row must exist before its dependency rows
    for existing in session.scalars(
        select(models.TaskDependency).where(
            models.TaskDependency.user_id == user_id, models.TaskDependency.task_id == row.id
        )
    ):
        session.delete(existing)
    session.flush()
    for position, dependency in enumerate(payload.dependency_ids):
        session.add(models.TaskDependency(user_id=user_id, task_id=row.id, position=position, depends_on_id=dependency))


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
        mutator.tombstone(PLACEMENTS, placement)


# -- fixed blocks ---------------------------------------------------------


def _block_content(_session, _user_id, row) -> dict:
    return {name: getattr(row, name) for name in FixedBlockFields.model_fields}


def _generic_assign(schema: type[BaseModel]):
    def assign(_session, _user_id, row, payload) -> None:
        for name, value in _fields(payload, schema).items():
            setattr(row, name, value)

    return assign


# -- placements -------------------------------------------------------------


def _placement_content(_session, _user_id, row) -> dict:
    return {name: getattr(row, name) for name in PlacementFields.model_fields}


def _placement_validate(session, user_id, payload: PlacementFields, existing) -> None:
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
    overrides = PreferenceOverrides.model_validate({**row.overrides, "optimizer_mode": row.optimizer_mode})
    return {"scope": row.scope, "date": row.scope_date, "overrides": overrides}


def _preference_assign(_session, _user_id, row, payload: PreferenceFields) -> None:
    row.scope, row.scope_date, row.scope_key = payload.scope, payload.date, _preference_scope_key(payload)
    mode = payload.overrides.optimizer_mode
    row.optimizer_mode = mode.value if mode is not None else None
    row.overrides = json.loads(overrides_to_document(payload.overrides))


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
    content=_block_content, assign=_generic_assign(FixedBlockFields),
)
PLACEMENTS = ResourceSpec(
    "placements", "placement", "placement", models.Placement, PlacementCreate, PlacementUpdate, PlacementOut,
    content=_placement_content, assign=_generic_assign(PlacementFields), validate=_placement_validate,
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
