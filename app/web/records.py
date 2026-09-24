"""
app/web/records.py

The record endpoints of the local web profile -- /projects, /tasks,
/fixed-blocks and /preferences -- with exactly the paths, request schemas,
response schemas (backend/resources.py) and error shapes of the hosted API,
so the web client is the same for both profiles. They are thin calls into
the scoped PlanningService: owner, references, versions, the fixed-block
invariants and the deletion policy are its rules, not re-implemented here.

Differences from the hosted server, by design: versions are this device's
local edit revisions (docs/sync-contract.md), placements are read through
/planning/snapshot and written only by generation, and executions are not
exposed by the local profile yet.
"""

import base64
import binascii
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.planning.errors import VersionConflictError
from app.planning.models import FixedBlock, Project, Task
from app.planning.preferences import PreferenceScope
from backend.api import page_model
from backend.errors import ApiError, not_found
from backend.planning_api import PlanningContext, _run, block_out, preference_out, project_out, task_out
from backend.resources import (
    FixedBlockCreate,
    FixedBlockFields,
    FixedBlockOut,
    FixedBlockUpdate,
    PreferenceCreate,
    PreferenceOut,
    PreferenceUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TaskCreate,
    TaskFields,
    TaskOut,
    TaskUpdate,
)

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class RecordKind:
    path: str
    label: str
    create_schema: type[BaseModel]
    update_schema: type[BaseModel]
    out_schema: type[BaseModel]
    #: (service, include_deleted) -> records
    list_all: Callable
    #: (service, id, include_deleted) -> record | None
    get: Callable
    to_out: Callable
    #: (service, owner, id, payload) -> record
    create: Callable
    #: (service, owner, stored, payload) -> record
    update: Callable
    #: (service, stored, base_version) -> None
    delete: Callable


def _task_model(owner, record_id, payload) -> Task:
    return Task(id=record_id, user_id=owner, **payload.model_dump(include=set(TaskFields.model_fields)))


def _block_model(owner, record_id, payload) -> FixedBlock:
    return FixedBlock(id=record_id, user_id=owner, **payload.model_dump(include=set(FixedBlockFields.model_fields)))


def _create_preference(service, owner, record_id, payload):
    scope = PreferenceScope(payload.scope)
    existing = service.user_preferences() if scope == PreferenceScope.USER else service.date_preferences(payload.date)
    if existing is not None:
        raise ApiError(409, "already_exists", "A preference layer for this scope already exists.",
                       current=preference_out(existing).model_dump(mode="json"))
    if scope == PreferenceScope.USER:
        return service.save_user_preferences(payload.overrides)
    return service.save_date_preferences(payload.date, payload.overrides)


def _update_preference(service, owner, stored, payload):
    if PreferenceScope(payload.scope) != stored.scope or payload.date != stored.date:
        raise ApiError(422, "validation_error", "A preference layer's scope and date cannot be changed.")
    if stored.scope == PreferenceScope.USER:
        return service.save_user_preferences(payload.overrides, expected_version=payload.base_version)
    return service.save_date_preferences(stored.date, payload.overrides, expected_version=payload.base_version)


def _delete_preference(service, stored, base_version):
    if stored.scope == PreferenceScope.USER:
        service.delete_user_preferences(expected_version=base_version)
    else:
        service.delete_date_preferences(stored.date, expected_version=base_version)


def _get_project(service, record_id, include_deleted):
    return next((p for p in service.list_projects(include_deleted=True) if p.id == record_id
                 and (include_deleted or p.deleted_at is None)), None)


KINDS = (
    RecordKind(
        "projects", "project", ProjectCreate, ProjectUpdate, ProjectOut,
        list_all=lambda service, deleted: service.list_projects(include_deleted=deleted),
        get=_get_project, to_out=project_out,
        create=lambda service, owner, record_id, payload: service.create_project(
            Project(id=record_id, user_id=owner, name=payload.name, description=payload.description)),
        update=lambda service, owner, stored, payload: service.update_project(
            stored.model_copy(update={"name": payload.name, "description": payload.description}),
            expected_version=payload.base_version),
        delete=lambda service, stored, base_version: service.delete_project(stored.id, expected_version=base_version),
    ),
    RecordKind(
        "tasks", "task", TaskCreate, TaskUpdate, TaskOut,
        list_all=lambda service, deleted: service.list_tasks(include_deleted=deleted),
        get=lambda service, record_id, deleted: (
            service.get_tasks_including_deleted([record_id]).get(record_id) if deleted else service.get_task(record_id)),
        to_out=task_out,
        create=lambda service, owner, record_id, payload: service.create_task(_task_model(owner, record_id, payload)),
        update=lambda service, owner, stored, payload: service.update_task(
            _task_model(owner, stored.id, payload), expected_version=payload.base_version),
        delete=lambda service, stored, base_version: service.delete_task(stored.id, expected_version=base_version),
    ),
    RecordKind(
        "fixed-blocks", "fixed block", FixedBlockCreate, FixedBlockUpdate, FixedBlockOut,
        list_all=lambda service, deleted: service.list_fixed_blocks(include_deleted=deleted),
        get=lambda service, record_id, deleted: service.get_fixed_block(record_id, include_deleted=deleted),
        to_out=block_out,
        create=lambda service, owner, record_id, payload: service.create_fixed_block(
            _block_model(owner, record_id, payload)),
        update=lambda service, owner, stored, payload: service.update_fixed_block(
            _block_model(owner, stored.id, payload), expected_version=payload.base_version),
        delete=lambda service, stored, base_version: service.delete_fixed_block(stored.id, expected_version=base_version),
    ),
    RecordKind(
        "preferences", "preference layer", PreferenceCreate, PreferenceUpdate, PreferenceOut,
        list_all=lambda service, deleted: service.preference_layers(include_deleted=deleted),
        get=lambda service, record_id, deleted: service.get_preference_layer(record_id, include_deleted=deleted),
        to_out=preference_out, create=_create_preference, update=_update_preference, delete=_delete_preference,
    ),
)


def _encode_cursor(record_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(record_id.bytes).decode().rstrip("=")


def _decode_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        raise ApiError(422, "validation_error", "The cursor is not valid.") from None


def _conflict_with_current(kind: RecordKind, service, record_id: uuid.UUID, error: VersionConflictError) -> ApiError:
    current = kind.get(service, record_id, True)
    return ApiError(
        409, "deleted" if error.deleted else "version_conflict", str(error),
        supplied_version=error.expected_version, current_version=error.current_version,
        current=kind.to_out(current).model_dump(mode="json") if current is not None else None,
    )


def _crud_router(kind: RecordKind, get_context: Callable[..., PlanningContext]) -> APIRouter:
    router = APIRouter(prefix=f"/{kind.path}", tags=[kind.path])

    def owner_of(context: PlanningContext):
        return context.service.owner_scope.user_id

    def load(context: PlanningContext, record_id: uuid.UUID, include_deleted: bool = False):
        record = kind.get(context.service, record_id, include_deleted)
        if record is None:
            raise not_found(kind.label)
        return record

    def list_records(
        limit: int | None = Query(default=None), cursor: str | None = Query(default=None),
        include_deleted: bool = Query(default=False), context: PlanningContext = Depends(get_context),
    ) -> dict[str, Any]:
        size = DEFAULT_PAGE_SIZE if limit is None else limit
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ApiError(422, "validation_error", f"limit must be between 1 and {MAX_PAGE_SIZE}.")
        records = sorted(_run(lambda: kind.list_all(context.service, include_deleted)), key=lambda r: r.id.bytes)
        if cursor:
            after = _decode_cursor(cursor).bytes
            records = [record for record in records if record.id.bytes > after]
        page, more = records[:size], len(records) > size
        return {"items": [kind.to_out(record) for record in page],
                "next_cursor": _encode_cursor(page[-1].id) if more else None}

    def read_record(record_id: uuid.UUID, include_deleted: bool = Query(default=False),
                    context: PlanningContext = Depends(get_context)):
        return kind.to_out(_run(lambda: load(context, record_id, include_deleted)))

    create_schema, update_schema = kind.create_schema, kind.update_schema

    def create_record(payload: create_schema, context: PlanningContext = Depends(get_context)):  # type: ignore[valid-type]
        record_id = payload.id or uuid.uuid4()
        return kind.to_out(_run(lambda: kind.create(context.service, owner_of(context), record_id, payload)))

    def update_record(record_id: uuid.UUID, payload: update_schema,  # type: ignore[valid-type]
                      context: PlanningContext = Depends(get_context)):
        def run():
            stored = load(context, record_id, include_deleted=True)
            if stored.deleted_at is not None:
                raise VersionConflictError(kind.label, record_id, expected_version=payload.base_version,
                                           current_version=stored.version, deleted=True)
            return kind.update(context.service, owner_of(context), stored, payload)

        try:
            return kind.to_out(_run(run))
        except ApiError as error:
            if error.code in ("version_conflict", "deleted"):
                raise _conflict_with_current(kind, context.service, record_id, VersionConflictError(
                    kind.label, record_id, expected_version=payload.base_version,
                    current_version=error.details.get("current_version"), deleted=error.code == "deleted")) from None
            raise

    def delete_record(record_id: uuid.UUID, base_version: int = Query(gt=0),
                      context: PlanningContext = Depends(get_context)):
        def run():
            stored = load(context, record_id, include_deleted=True)
            if stored.deleted_at is not None or stored.version != base_version:
                raise VersionConflictError(kind.label, record_id, expected_version=base_version,
                                           current_version=stored.version, deleted=stored.deleted_at is not None)
            kind.delete(context.service, stored, base_version)
            return load(context, record_id, include_deleted=True)

        try:
            return kind.to_out(_run(run))
        except ApiError as error:
            if error.code in ("version_conflict", "deleted"):
                raise _conflict_with_current(kind, context.service, record_id, VersionConflictError(
                    kind.label, record_id, expected_version=base_version,
                    current_version=error.details.get("current_version"), deleted=error.code == "deleted")) from None
            raise

    name = kind.path.replace("-", "_").rstrip("s")
    router.add_api_route("", list_records, methods=["GET"], response_model=page_model(kind.out_schema),
                         operation_id=f"local_list_{name}")
    router.add_api_route("/{record_id}", read_record, methods=["GET"], response_model=kind.out_schema,
                         operation_id=f"local_read_{name}")
    router.add_api_route("", create_record, methods=["POST"], status_code=201, response_model=kind.out_schema,
                         operation_id=f"local_create_{name}")
    router.add_api_route("/{record_id}", update_record, methods=["PUT"], response_model=kind.out_schema,
                         operation_id=f"local_update_{name}")
    router.add_api_route("/{record_id}", delete_record, methods=["DELETE"], response_model=kind.out_schema,
                         operation_id=f"local_delete_{name}")
    return router


def build_records_router(get_context: Callable[..., PlanningContext]) -> APIRouter:
    router = APIRouter()
    for kind in KINDS:
        router.include_router(_crud_router(kind, get_context))
    return router
