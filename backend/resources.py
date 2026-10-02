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
                 tombstones its live placements (each logged). Recurrence
                 (docs/recurrence.md): an occurrence names one of the user's
                 series and its id is derived from (series, slot); its
                 identity, and a segment's lineage, never change; a new
                 dependency on a series is series-to-series with a compatible
                 cadence (anything else names one concrete occurrence); an
                 occurrence may keep its same-slot edge to a skipped
                 prerequisite occurrence, and such edges never block a skip.
                 Fields an update omits (an older client that does not know
                 them) keep their stored values -- recurrence identity,
                 exception state, lineage and a series' anchor are never
                 erased by omission -- and an older client's content edit of
                 an occurrence marks it modified. Creating an identical
                 occurrence again (another device expanded the same slot) is
                 accepted as a no-op; a different one is a conflict. A new
                 live occurrence must still be a date of its series and --
                 unless it is an exception (modified) -- carry the series'
                 current content; otherwise, or when the series was deleted,
                 it is 409 series_changed with the series as `current`, so an
                 occurrence expanded offline from an older series never gets
                 around a concurrent series edit.
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
                 create or update; origin (generated/manual) and preserved
                 (manual intent) are kept when an update omits them (an older
                 client); an update never changes a known origin and can only
                 release manual intent (preserved true -> false), never grant
                 it -- a move's destination is created preserved
    preference   one live layer per scope ("user" or one date); scope is fixed
    generation   one live record per date; the date is fixed
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.planning import fixed_block_rules
from app.planning.models import FixedBlock as CanonicalFixedBlock
from app.planning.models import (
    DEFAULT_TASK_POINTS,
    MAX_TASK_POINTS,
    OCCURRENCE_TOMBSTONE_STATES,
    LocalTimeWindow,
    OccurrenceState,
    RecurrenceSpec,
)
from app.planning.models import PlacementOrigin, PlacementRemovalReason
from app.planning.recurrence import SeriesRule, cadence_problem, occurrence_task_id
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


#: Request bounds of a task's lists (validation errors, before any database work).
MAX_TAGS = 50
MAX_PREFERRED_DATES = 366
MAX_DEPENDENCIES = 200


class TaskFields(Strict):
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=MAX_TAGS)
    estimated_duration_minutes: int = Field(gt=0)
    priority: int = Field(ge=1, le=10)
    #: The user's productivity value (not the optimizer's placement score); omitted by older clients: the default.
    points: int = Field(default=DEFAULT_TASK_POINTS, ge=0, le=MAX_TASK_POINTS)
    required: bool = False
    required_date: date_ | None = None
    preferred_dates: list[date_] = Field(default_factory=list, max_length=MAX_PREFERRED_DATES)
    preferred_time_window: LocalTimeWindow | None = None
    dependency_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    deadline: AwareDatetime | None = None
    recurrence: RecurrenceSpec | None = None
    #: Recurrence identity, exception state, provenance and lineage (docs/recurrence.md). An older client
    #: omits them: an update then keeps the stored values (see with_stored_omissions).
    series_id: uuid.UUID | None = None
    occurrence_slot: date_ | None = None
    occurrence_state: OccurrenceState | None = None
    series_version: int | None = Field(default=None, gt=0)
    series_predecessor_id: uuid.UUID | None = None

    def canonical(self, task_id: uuid.UUID) -> CanonicalTask:
        data = self.model_dump(include=set(TaskFields.model_fields))
        if self.occurrence_state in OCCURRENCE_TOMBSTONE_STATES:
            data["deleted_at"] = datetime.now(timezone.utc)  # only ever stored as a tombstone
        return CanonicalTask(id=task_id, **data)

    @model_validator(mode="after")
    def _canonical_rules(self):
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("dependency_ids must not repeat a task")
        task_id = getattr(self, "id", None)
        if task_id is None and self.series_id is not None and self.occurrence_slot is not None:
            task_id = occurrence_task_id(self.series_id, self.occurrence_slot)
        _validated(lambda: self.canonical(task_id or uuid.uuid4()))
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
    #: How it came to be (generated / manual; None = unknown) and the user's manual intent: generation keeps a
    #: preserved placement until it is released (docs/execution-rescheduling.md, "Manual placements").
    origin: PlacementOrigin | None = None
    preserved: bool = False

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


#: Task fields an update may omit (an older client does not know them): omitted ones keep the stored values.
_KEPT_WHEN_OMITTED = ("series_id", "occurrence_slot", "occurrence_state", "series_version", "series_predecessor_id")
#: An occurrence's bookkeeping, not its content (a change of anything else is an edit of it).
_OCCURRENCE_BOOKKEEPING = {"occurrence_state", "series_version"}


def with_stored_omissions(row, payload: TaskFields) -> TaskFields:
    """
    The update `payload` with every recurrence field it omits taken from the
    stored `row` (docs/recurrence.md, "older clients"): an older client's
    update can never erase an occurrence's identity, exception state,
    provenance or a series' lineage and anchor -- and its content edit of an
    occurrence that still followed its series marks it modified.
    """
    stored = task_content(row)
    sent = payload.model_fields_set
    update = {name: stored[name] for name in _KEPT_WHEN_OMITTED if name not in sent}
    recurrence = payload.recurrence
    if (recurrence is not None and stored["recurrence"] is not None
            and not {"start_date", "timezone"} & recurrence.model_fields_set):
        update["recurrence"] = recurrence.model_copy(update={
            "start_date": stored["recurrence"]["start_date"], "timezone": stored["recurrence"]["timezone"]})
    if not update:
        return payload
    merged = payload.model_copy(update=update)
    if "occurrence_state" not in sent and stored["series_id"] is not None and stored["occurrence_state"] is None:
        before = {key: value for key, value in TaskFields.model_validate(stored).model_dump().items()
                  if key not in _OCCURRENCE_BOOKKEEPING}
        after = {key: value for key, value in merged.model_dump(include=set(TaskFields.model_fields)).items()
                 if key not in _OCCURRENCE_BOOKKEEPING}
        if before != after:
            merged = merged.model_copy(update={"occurrence_state": OccurrenceState.MODIFIED})
    return merged


def _task_assign(session, _user_id, row, payload: TaskFields) -> None:
    if session is not None and not inspect(row).pending:
        payload = with_stored_omissions(row, payload)  # an update of a stored row
    write_task(row, payload)


def _task_validate(session, user_id, payload: TaskFields, existing) -> None:
    if existing is not None:
        payload = with_stored_omissions(existing, payload)
    if payload.project_id is not None and live(session, models.Project, user_id, payload.project_id) is None:
        raise invalid_reference("project_id does not name one of your projects.")
    if existing is not None:
        if (existing.series_id, existing.occurrence_slot) != (payload.series_id, payload.occurrence_slot):
            raise ApiError(422, "validation_error", "An occurrence's series and original slot never change.")
        if existing.series_predecessor_id != payload.series_predecessor_id:
            raise ApiError(422, "validation_error", "A series segment's lineage never changes.")
        if existing.recurrence_frequency is not None and payload.recurrence is None and session.scalars(
                select(models.Task.id).where(models.Task.user_id == user_id, models.Task.series_id == existing.id)
                .limit(1)).first() is not None:
            raise ApiError(422, "validation_error", "This series has occurrences, so it stays a recurring series; end "
                                                    "or delete the series instead.")
    elif payload.series_id is not None:
        series = session.get(models.Task, (user_id, payload.series_id))
        tombstone = payload.occurrence_state in OCCURRENCE_TOMBSTONE_STATES
        if series is None or series.recurrence_frequency is None:
            raise invalid_reference("series_id must name one of your live recurring series.")
        if not tombstone:
            _check_series_precondition(session, user_id, series, payload)
    if existing is None and payload.series_predecessor_id is not None:
        predecessor = session.get(models.Task, (user_id, payload.series_predecessor_id))
        if predecessor is None or predecessor.recurrence_frequency is None:
            raise invalid_reference("series_predecessor_id must name one of your recurring series.")
    stored_edges = {item.depends_on_id for item in existing.dependency_rows} if existing is not None else set()
    for dependency in payload.dependency_ids:
        if existing is not None and dependency == existing.id:
            raise ApiError(422, "validation_error", "A task cannot depend on itself.")
        target = session.get(models.Task, (user_id, dependency))
        if target is None or (target.deleted_at is not None and not (
                target.series_id is not None and payload.series_id is not None)):
            raise invalid_reference("dependency_ids must name your own existing tasks.")
        if dependency in stored_edges or target.recurrence_frequency is None:
            continue
        if payload.recurrence is None:
            raise invalid_reference(f"A one-off task or an occurrence cannot depend on the recurring series "
                                    f"{target.name!r} itself; name one concrete occurrence of it.")
        target_spec = RecurrenceSpec.model_validate(task_content(target)["recurrence"])
        if payload.recurrence.configured and target_spec.configured:
            problem = cadence_problem(SeriesRule.of(payload.recurrence), SeriesRule.of(target_spec))
            if problem is not None:
                raise invalid_reference(f"This series cannot depend on the series {target.name!r}: {problem}")
    if payload.recurrence is not None and payload.dependency_ids:
        task_id = existing.id if existing is not None else getattr(payload, "id", None)
        _refuse_series_cycles(session, user_id, task_id, payload.dependency_ids)


#: The occurrence fields its series dictates (app/planning/series.py: _CONTENT_FIELDS, occurrence_for).
_SERIES_CONTENT = frozenset({"project_id", "name", "category", "tags", "estimated_duration_minutes", "priority",
                             "points", "required", "preferred_time_window"})


def _check_series_precondition(session, user_id, series, payload: TaskFields) -> None:
    """
    The series precondition of a new live occurrence (docs/sync-protocol.md,
    "Recurring series"): its series is live, the slot is still one of the
    series' dates, and an occurrence that follows its series (no exception
    state) carries the series' current content. Versions cannot be compared
    -- a device numbers its own versions -- so the content is: an occurrence
    expanded offline from an older series is refused as 409 series_changed
    with the series as `current`, never stored as a stale copy.
    """
    content = task_content(series)
    reason = None
    if series.deleted_at is not None:
        reason = "the series was deleted"
    else:
        spec = RecurrenceSpec.model_validate(content["recurrence"])
        if not spec.configured or not SeriesRule.of(spec).is_slot(payload.occurrence_slot):
            reason = f"{payload.occurrence_slot} is no longer one of its dates"
        elif payload.occurrence_state is None:
            stored = TaskFields.model_validate(content).model_dump(include=_SERIES_CONTENT)
            if stored != payload.model_dump(include=_SERIES_CONTENT):
                reason = "its details changed after this occurrence was created"
    if reason is not None:
        raise ApiError(409, "series_changed", f"The recurring series changed on the server: {reason}. Accept the "
                                              "server's series; the occurrence then follows it.",
                       current=TASKS.serialize(session, user_id, series))


def _refuse_series_cycles(session, user_id, task_id, dependency_ids) -> None:
    """Series definitions never depend on each other in a cycle (every slot would be a cycle)."""
    if task_id is None:
        return
    edges: dict[uuid.UUID, set[uuid.UUID]] = {}
    for depender, depends_on in session.execute(
        select(models.TaskDependency.task_id, models.TaskDependency.depends_on_id)
        .join(models.Task, (models.Task.user_id == models.TaskDependency.user_id)
              & (models.Task.id == models.TaskDependency.task_id))
        .where(models.TaskDependency.user_id == user_id, models.Task.recurrence_frequency.is_not(None),
               models.Task.deleted_at.is_(None))
    ):
        edges.setdefault(depender, set()).add(depends_on)
    edges[task_id] = set(dependency_ids)
    seen: set[uuid.UUID] = set()
    stack = list(edges.get(task_id, ()))
    while stack:
        current = stack.pop()
        if current == task_id:
            raise ApiError(422, "validation_error", "Recurring series cannot depend on each other in a cycle.")
        if current not in seen:
            seen.add(current)
            stack.extend(edges.get(current, ()))


def _task_before_delete(mutator, row, occurrence_state: str | None = None) -> None:
    rows = mutator.session.execute(
        select(models.TaskDependency.task_id, models.Task.series_id)
        .join(models.Task, (models.Task.user_id == models.TaskDependency.user_id)
              & (models.Task.id == models.TaskDependency.task_id))
        .where(
            models.TaskDependency.user_id == mutator.user_id, models.TaskDependency.depends_on_id == row.id,
            models.Task.deleted_at.is_(None),
        )
    ).all()
    # An occurrence's same-slot edge never blocks skipping/deleting its prerequisite occurrence.
    blocking = [task_id for task_id, series_id in rows if not (row.series_id is not None and series_id is not None)]
    if blocking:
        raise ApiError(409, "in_use", "Other tasks depend on this task; remove those dependencies first.")
    if row.series_id is not None:
        if occurrence_state is not None and occurrence_state not in {state.value for state in OCCURRENCE_TOMBSTONE_STATES}:
            raise ApiError(422, "validation_error", "An occurrence is removed as skipped, deleted or superseded.")
        row.occurrence_state = occurrence_state or OccurrenceState.DELETED.value
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


#: Placement fields an update may omit (an older client does not know them): omitted ones keep the stored values.
_PLACEMENT_KEPT_WHEN_OMITTED = ("origin", "preserved")


def placement_with_stored_omissions(row, payload: PlacementFields) -> PlacementFields:
    """The update `payload` with the origin and manual intent it omits taken from the stored `row`."""
    sent = payload.model_fields_set
    stored = {"origin": PlacementOrigin(row.origin) if row.origin is not None else None,
              "preserved": bool(row.preserved)}
    update = {name: stored[name] for name in _PLACEMENT_KEPT_WHEN_OMITTED if name not in sent}
    return payload.model_copy(update=update) if update else payload


def _placement_assign(session, user_id, row, payload: PlacementFields) -> None:
    """
    The payload's content. task_category is a snapshot: kept when the payload
    has none, and taken from the task's current category when a live
    placement is created without one (a revision row stores exactly what it
    is given: it is filled without a session).
    """
    if session is not None and not inspect(row).pending:
        payload = placement_with_stored_omissions(row, payload)  # an update of a stored row
    for name, value in _fields(payload, PlacementFields).items():
        if name == "task_category":
            continue
        setattr(row, name, value.value if isinstance(value, (PlacementRemovalReason, PlacementOrigin)) else value)
    if payload.task_category is not None:
        row.task_category = payload.task_category
    elif session is not None and inspect(row).pending and row.deleted_at is None:
        task = session.get(models.Task, (user_id, payload.task_id))
        row.task_category = task.category if task is not None else None


def _placement_validate(session, user_id, payload: PlacementFields, existing) -> None:
    if payload.removal_reason is not None or payload.superseded_by_id is not None:
        raise ApiError(422, "validation_error", "removal_reason and superseded_by_id are recorded when a placement "
                                                "is removed; a create or update cannot set them.")
    if existing is not None:
        payload = placement_with_stored_omissions(existing, payload)
        origin = payload.origin.value if payload.origin is not None else None
        if existing.origin is not None and origin != existing.origin:
            raise ApiError(422, "validation_error", "A placement's origin never changes once it is known.")
        if payload.preserved and not existing.preserved:
            raise ApiError(422, "validation_error", "Manual intent is recorded by a move (reschedule); an update can "
                                                    "only release it.")
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
