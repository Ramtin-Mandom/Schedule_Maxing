"""
backend/api.py

HTTP routes. Every protected route depends on `current_user_id`, which
derives the user exclusively from a verified access token, and every query
below is filtered by that user id -- collections, single reads, mutations,
nested relationships, and the change log. A record of another user is
indistinguishable from one that does not exist (404).

Collections are bounded and paginated with an opaque keyset cursor
(`next_cursor`), ordered by id. Tombstones are only returned with
include_deleted=true.
"""

import base64
import binascii
import unicodedata
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.database import session_scope
from backend.errors import ApiError, not_found, unauthenticated, version_conflict
from backend.executions import ACTIONS, EXECUTIONS, ActionIn, ExecutionCreate, ExecutionOut, FeedbackIn
from backend.migrate import current_revision, head_revision
from backend.mutations import mutation
from backend.resources import CRUD_RESOURCES, ResourceSpec
from backend.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    TokenError,
    hash_password,
    issue_access_token,
    needs_rehash,
    normalize_identifier,
    verify_access_token,
    verify_password,
)
from backend.settings import BackendSettings

# -----------------------------------------------------------------------------
# Dependencies
# -----------------------------------------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_settings(request: Request) -> BackendSettings:
    return request.app.state.settings


def server_now(request: Request) -> datetime:
    return request.app.state.clock()


_bearer = HTTPBearer(auto_error=False, description="An access token from POST /auth/login.")


def current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> uuid.UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthenticated()
    try:
        user_id = verify_access_token(credentials.credentials, request.app.state.settings, request.app.state.clock())
    except TokenError:
        raise unauthenticated("The access token is invalid or has expired.") from None
    if session.get(models.User, user_id) is None:
        raise unauthenticated("The access token is invalid or has expired.")
    return user_id


# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------

health = APIRouter(tags=["health"])


@health.get("/health", summary="Liveness: the process is serving requests (no database access).")
def liveness() -> dict:
    return {"status": "ok"}


@health.get("/ready", summary="Readiness: the database is reachable and fully migrated.")
def readiness(request: Request, response: Response) -> dict:
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = current_revision(connection)
    except Exception:  # noqa: BLE001 - any database failure means "not ready"; details are not exposed
        response.status_code = 503
        return {"status": "unavailable", "database": "unreachable"}
    head = head_revision()
    if current != head:
        response.status_code = 503
        return {"status": "unavailable", "database": "reachable", "migrations": "pending",
                "current_revision": current, "head_revision": head}
    return {"status": "ready", "database": "reachable", "migrations": "current", "revision": current}


# -----------------------------------------------------------------------------
# Accounts
# -----------------------------------------------------------------------------

auth = APIRouter(tags=["accounts"])


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    username: str | None = Field(default=None, min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    display_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _email_shape(self):
        email = normalize_identifier(self.email)
        local, _, domain = email.partition("@")
        if not local or "." not in domain or " " in email:
            raise ValueError("email must be an email address")
        return self


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str | None = Field(default=None, max_length=320)
    username: str | None = Field(default=None, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @model_validator(mode="after")
    def _one_identifier(self):
        if (self.email is None) == (self.username is None):
            raise ValueError("give exactly one of email or username")
        return self


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: datetime


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int = Field(gt=0)
    display_name: str | None = Field(default=None, max_length=200)


def _user_out(user: models.User) -> dict:
    return UserOut.model_validate(user, from_attributes=True).model_dump(mode="json")


@auth.post("/auth/register", status_code=201, response_model=UserOut, summary="Create an account.")
def register(payload: RegisterIn, session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    user = models.User(
        id=uuid.uuid4(),
        email=normalize_identifier(payload.email),
        username=normalize_identifier(payload.username) if payload.username else None,
        display_name=unicodedata.normalize("NFKC", payload.display_name).strip() if payload.display_name else None,
        password_hash=hash_password(payload.password),
        change_seq=0, created_at=now, updated_at=now, version=1,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        # The unique constraints decide, so two concurrent registrations cannot both succeed.
        session.rollback()
        raise ApiError(409, "account_exists", "An account with this email or username already exists.") from None
    return _user_out(user)


@auth.post("/auth/login", response_model=TokenOut, summary="Exchange credentials for an access token.")
def login(
    payload: LoginIn,
    request: Request,
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    column = models.User.email if payload.email is not None else models.User.username
    user = session.scalars(select(models.User).where(
        column == normalize_identifier(payload.email or payload.username)
    )).first()
    if not verify_password(payload.password, user.password_hash if user else None):
        raise unauthenticated("The email/username or password is incorrect.")
    if needs_rehash(user.password_hash):
        session.execute(update(models.User).where(models.User.id == user.id)
                        .values(password_hash=hash_password(payload.password)))
        session.commit()
    issued = issue_access_token(user.id, request.app.state.settings, now)
    return {"access_token": issued.token, "expires_in": issued.expires_in, "expires_at": issued.expires_at}


@auth.get("/me", response_model=UserOut, summary="The authenticated account.")
def me(user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session)) -> dict:
    return _user_out(session.get(models.User, user_id))


@auth.patch("/me", response_model=UserOut, summary="Update the authenticated account's profile.")
def update_me(
    payload: ProfileUpdate,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    user = session.get(models.User, user_id, populate_existing=True)
    if user.version != payload.base_version:
        raise ApiError(409, "version_conflict", "The profile was changed since that version.",
                       supplied_version=payload.base_version, current_version=user.version, current=_user_out(user))
    name = unicodedata.normalize("NFKC", payload.display_name).strip() if payload.display_name else None
    if name != user.display_name:
        user.display_name, user.updated_at, user.version = name, now, user.version + 1
        session.commit()
    return _user_out(user)


# -----------------------------------------------------------------------------
# Resources
# -----------------------------------------------------------------------------


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


def _encode_cursor(record_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(record_id.bytes).decode().rstrip("=")


def _decode_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        raise ApiError(422, "validation_error", "The cursor is not valid.") from None


def _page_size(request: Request, limit: int | None) -> int:
    settings = request.app.state.settings
    if limit is None:
        return settings.default_page_size
    if not 1 <= limit <= settings.max_page_size:
        raise ApiError(422, "validation_error", f"limit must be between 1 and {settings.max_page_size}.")
    return limit


def _list(session: Session, spec, user_id: uuid.UUID, limit: int, cursor: str | None, include_deleted: bool) -> dict:
    query = select(spec.model).where(spec.model.user_id == user_id)
    if not include_deleted:
        query = query.where(spec.model.deleted_at.is_(None))
    if cursor:
        query = query.where(spec.model.id > _decode_cursor(cursor))
    rows = list(session.scalars(query.order_by(spec.model.id).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [spec.serialize(session, user_id, row) for row in rows],
        "next_cursor": _encode_cursor(rows[-1].id) if more else None,
    }


def _read(session: Session, spec, user_id: uuid.UUID, record_id: uuid.UUID, include_deleted: bool) -> dict:
    row = session.get(spec.model, (user_id, record_id))
    if row is None or (row.deleted_at is not None and not include_deleted):
        raise not_found(spec.label)
    return spec.serialize(session, user_id, row)


def _crud_router(spec: ResourceSpec) -> APIRouter:
    router = APIRouter(prefix=f"/{spec.path}", tags=[spec.path])
    create_schema, update_schema, out_schema = spec.create_schema, spec.update_schema, spec.out_schema

    def list_records(
        request: Request,
        limit: int | None = Query(default=None, description="Page size (default 100, bounded by API_MAX_PAGE_SIZE)."),
        cursor: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        user_id: uuid.UUID = Depends(current_user_id),
        session: Session = Depends(get_session),
    ) -> dict:
        return _list(session, spec, user_id, _page_size(request, limit), cursor, include_deleted)

    def read_record(
        record_id: uuid.UUID,
        include_deleted: bool = Query(default=False),
        user_id: uuid.UUID = Depends(current_user_id),
        session: Session = Depends(get_session),
    ) -> dict:
        return _read(session, spec, user_id, record_id, include_deleted)

    def create_record(
        payload: create_schema,  # type: ignore[valid-type]
        request: Request,
        user_id: uuid.UUID = Depends(current_user_id),
        session: Session = Depends(get_session),
    ) -> dict:
        with mutation(session, user_id, request.app.state.clock) as mutator:
            return mutator.create(spec, payload)

    def update_record(
        record_id: uuid.UUID,
        payload: update_schema,  # type: ignore[valid-type]
        request: Request,
        user_id: uuid.UUID = Depends(current_user_id),
        session: Session = Depends(get_session),
    ) -> dict:
        with mutation(session, user_id, request.app.state.clock) as mutator:
            return mutator.update(spec, record_id, payload)

    def delete_record(
        record_id: uuid.UUID,
        request: Request,
        base_version: int = Query(gt=0, description="The version the deletion is based on (precondition)."),
        user_id: uuid.UUID = Depends(current_user_id),
        session: Session = Depends(get_session),
    ) -> dict:
        with mutation(session, user_id, request.app.state.clock) as mutator:
            return mutator.delete(spec, record_id, base_version)

    name = spec.entity_type
    router.add_api_route("", list_records, methods=["GET"], response_model=Page, operation_id=f"list_{name}")
    router.add_api_route("/{record_id}", read_record, methods=["GET"], response_model=out_schema,
                         operation_id=f"read_{name}")
    router.add_api_route("", create_record, methods=["POST"], status_code=201, response_model=out_schema,
                         operation_id=f"create_{name}")
    router.add_api_route("/{record_id}", update_record, methods=["PUT"], response_model=out_schema,
                         operation_id=f"update_{name}")
    router.add_api_route("/{record_id}", delete_record, methods=["DELETE"], response_model=out_schema,
                         operation_id=f"delete_{name}")
    return router


executions = APIRouter(prefix="/executions", tags=["executions"])


@executions.get("", response_model=Page, operation_id="list_execution")
def list_executions(
    request: Request,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return _list(session, EXECUTIONS, user_id, _page_size(request, limit), cursor, include_deleted)


@executions.get("/{record_id}", response_model=ExecutionOut, operation_id="read_execution")
def read_execution(
    record_id: uuid.UUID,
    include_deleted: bool = Query(default=False),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return _read(session, EXECUTIONS, user_id, record_id, include_deleted)


@executions.post("", status_code=201, response_model=ExecutionOut, operation_id="create_execution")
def create_execution(
    payload: ExecutionCreate,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return mutator.create_execution(payload)


@executions.post("/{record_id}/actions/{action}", response_model=ExecutionOut, operation_id="execution_action")
def execution_action(
    record_id: uuid.UUID,
    action: str,
    payload: ActionIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    if action not in ACTIONS:
        raise ApiError(404, "not_found", f"Unknown action; use one of {', '.join(ACTIONS)}.")
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return mutator.execution_action(record_id, action, payload)


@executions.post("/{record_id}/feedback", response_model=ExecutionOut, operation_id="execution_feedback")
def execution_feedback(
    record_id: uuid.UUID,
    payload: FeedbackIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return mutator.execution_feedback(record_id, payload)


@executions.delete("/{record_id}", response_model=ExecutionOut, operation_id="delete_execution")
def delete_execution(
    record_id: uuid.UUID,
    request: Request,
    base_version: int = Query(gt=0),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return mutator.delete_execution(record_id, base_version)


# -----------------------------------------------------------------------------
# Change log
# -----------------------------------------------------------------------------

changes = APIRouter(tags=["changes"])


class ChangeOut(BaseModel):
    seq: int
    entity_type: str
    entity_id: uuid.UUID
    operation: str
    version: int
    recorded_at: datetime
    record: dict[str, Any]


class ChangePage(BaseModel):
    changes: list[ChangeOut]
    #: Pass as `after` to continue; equal to the last returned seq (or the given `after` when nothing is new).
    cursor: int
    has_more: bool


@changes.get("/changes", response_model=ChangePage, summary="The caller's accepted changes, in commit order.")
def list_changes(
    request: Request,
    after: int = Query(default=0, ge=0, description="Return changes with seq greater than this."),
    limit: int | None = Query(default=None),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    size = _page_size(request, limit)
    rows = list(session.scalars(
        select(models.ChangeLogEntry)
        .where(models.ChangeLogEntry.user_id == user_id, models.ChangeLogEntry.seq > after)
        .order_by(models.ChangeLogEntry.seq).limit(size + 1)
    ))
    more = len(rows) > size
    rows = rows[:size]
    return {
        "changes": [
            {"seq": row.seq, "entity_type": row.entity_type, "entity_id": row.entity_id, "operation": row.operation,
             "version": row.version, "recorded_at": row.recorded_at, "record": row.payload}
            for row in rows
        ],
        "cursor": rows[-1].seq if rows else after,
        "has_more": more,
    }


def install_routes(app: FastAPI) -> None:
    app.include_router(health)
    app.include_router(auth)
    for spec in CRUD_RESOURCES:
        app.include_router(_crud_router(spec))
    app.include_router(executions)
    app.include_router(changes)


__all__ = ["install_routes", "current_user_id", "version_conflict"]
