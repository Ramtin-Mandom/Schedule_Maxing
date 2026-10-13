"""
backend/api.py

HTTP routes. Every protected route depends on `current_user_id`, which
derives the user exclusively from a verified access token (Authorization:
Bearer) or a live browser session cookie (backend/browser_sessions.py, with
its CSRF and origin checks), and every query
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
from datetime import date as date_
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend import account_security, browser_sessions, models, security_events, snapshots
from backend.accounts import AccountIdentity, AccountService, InvalidCredentialsError
from backend.auth_schemas import RefreshIn, TokenPairOut
from backend.native_sessions import NativeSessionService, require_live_session
from backend.database import session_scope
from backend.errors import ApiError, not_found, unauthenticated, version_conflict
from backend.executions import ACTIONS, EXECUTIONS, ActionIn, ExecutionCreate, ExecutionOut, FeedbackIn
from backend.migrate import current_revision, head_revision
from backend.mutations import mutation
from backend.days import DayOutcomeIn, DayOutcomeOut, day_summaries, set_day_outcome
from backend.task_data_reset import ResetTaskDataIn, ResetTaskDataOut, reset_task_data
from backend.outcomes import OutcomeIn, OutcomeOut, set_placement_outcome
from backend.resources import CRUD_RESOURCES, ResourceSpec
from backend.mfa import MfaService
from backend.security import (
    MAX_PASSWORD_LENGTH,
    MFA_TOKEN_TTL_SECONDS,
    TokenError,
    issue_mfa_token,
    verify_access_identity,
)
from backend.login_guard import LoginGuard
from backend.protection import client_address, throttle_address
from backend.recovery import credential_epoch
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


def throttle(request: Request, scope: str, subject: str | None = None) -> None:
    """
    Count this request against the shared limit of `scope` (backend/rate_limit.py) -- per client address
    unless `subject` names another subject. A no-op when rate limiting is disabled.
    """
    limiter = request.app.state.rate_limiter
    if limiter is None:
        return
    limiter.hit(scope, subject if subject is not None else address_subject(request))


def address_subject(request: Request) -> str:
    """The limiter subject of this request's client address (backend/protection.py decides the address)."""
    settings = request.app.state.settings
    return "ip:" + throttle_address(client_address(request, settings.trusted_proxies, settings.client_ip_header))


def current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> uuid.UUID:
    if credentials is None and request.cookies.get(browser_sessions.COOKIE_NAME):
        user_id = browser_sessions.authenticate(request, session, request.app.state.clock())
        if session.get(models.User, user_id) is None:
            raise unauthenticated("You are signed out. Sign in again.")
        require_verified_email(request, session, user_id)
        return user_id
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthenticated()
    try:
        user_id, epoch, family_id = verify_access_identity(credentials.credentials, request.app.state.settings,
                                                          request.app.state.clock())
    except TokenError:
        raise unauthenticated("The access token is invalid or has expired.") from None
    # Checked against the stored account on every request: a password reset (a new epoch) ends every older token.
    if credential_epoch(session, user_id) != epoch:
        raise unauthenticated("The access token is invalid or has expired.")
    if family_id is not None:
        require_live_session(session, family_id, user_id, epoch, request.app.state.clock())
        request.state.session_ref = ("app", family_id)
    require_verified_email(request, session, user_id)
    return user_id


def require_verified_email(request: Request, session: Session, user_id: uuid.UUID) -> None:
    """
    With REQUIRE_VERIFIED_EMAIL, an account whose address was never verified reaches only its own account
    routes (/me and /auth/...: enough to verify, sign out or reset) -- no records.
    """
    if not request.app.state.settings.require_verified_email:
        return
    path = request.url.path
    if path == "/me" or path.startswith("/auth/"):
        return
    verified_at = session.scalar(select(models.User.email_verified_at).where(models.User.id == user_id))
    if verified_at is None:
        raise ApiError(403, "email_unverified", "Confirm your email address to use your records.")


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
    # Public, so minimal: whether it is ready and which stage is not -- never the schema revision or any
    # other detail of the installation (`python -m backend.migrate check` reports those to the operator).
    if current != head_revision():
        response.status_code = 503
        return {"status": "unavailable", "database": "reachable", "migrations": "pending"}
    return {"status": "ready", "database": "reachable", "migrations": "current"}


# -----------------------------------------------------------------------------
# Accounts
# -----------------------------------------------------------------------------

auth = APIRouter(tags=["accounts"])


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


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    #: False: the address was never proved to be the account owner's (docs/email.md, "Existing accounts").
    email_verified: bool = False


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int = Field(gt=0)
    display_name: str | None = Field(default=None, max_length=200)


def _user_out(user: models.User | AccountIdentity) -> dict:
    return UserOut.model_validate(user, from_attributes=True).model_dump(mode="json")


def _check_credentials(payload: LoginIn, session: Session, request: Request) -> AccountIdentity:
    throttle(request, "login_ip")  # the cheap request limit, before any lookup or hashing
    service = AccountService(session)
    account = service.account_subject(email=payload.email, username=payload.username)
    session.rollback()  # the lookup was a read; the limiter works in transactions of its own
    guard = LoginGuard(request.app.state.rate_limiter, address_subject(request), account)
    guard.before()  # failures already used up: refused without verifying a password
    try:
        return service.authenticate(email=payload.email, username=payload.username, password=payload.password)
    except InvalidCredentialsError as error:
        session.rollback()
        security_events.record("sign_in_failed")
        guard.failed()
        raise unauthenticated(str(error)) from None


class MfaChallengeOut(BaseModel):
    #: The password was right and the account has two-step sign-in: finish with POST /auth/mfa/verify.
    mfa_required: bool = True
    mfa_token: str
    expires_in: int


@auth.post("/auth/login", response_model=TokenPairOut | MfaChallengeOut,
           summary="Create a native session and rotating token pair (or ask for the second step).")
def login(
    payload: LoginIn,
    request: Request,
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    user = _check_credentials(payload, session, request)
    return mfa_challenge(request, session, user, now) or start_app_session(request, session, user, now)


def mfa_service(request: Request, session: Session, now: datetime) -> MfaService:
    settings = request.app.state.settings
    return MfaService(session, lambda: now, request.app.state.mfa_cipher, settings.mfa_issuer)


def mfa_challenge(request: Request, session: Session, user: AccountIdentity, now: datetime) -> dict | None:
    """
    For an account with two-step sign-in: the answer to a correct password -- a short-lived token good only for
    the second step. No session, cookie or access token exists until that step succeeds.
    """
    enabled = mfa_service(request, session, now).enabled(user.id)
    session.rollback()
    if not enabled:
        return None
    return {"mfa_required": True, "expires_in": MFA_TOKEN_TTL_SECONDS,
            "mfa_token": issue_mfa_token(user.id, request.app.state.settings, now, user.credential_epoch)}


def signed_in(request: Request, session: Session, user: AccountIdentity, now: datetime) -> str | None:
    """A sign-in completed (every step): remember where from, notify a newly recognized place; the client label."""
    settings = request.app.state.settings
    network = throttle_address(client_address(request, settings.trusted_proxies, settings.client_ip_header))
    user_agent = request.headers.get("user-agent")
    account_security.note_sign_in(session, user.id, user.email, network, user_agent, now)
    return account_security.client_label(user_agent)


def start_app_session(request: Request, session: Session, user: AccountIdentity, now: datetime) -> dict:
    label = signed_in(request, session, user, now)
    return NativeSessionService(session, request.app.state.settings, now).start(user.id, user.credential_epoch, label)


def start_browser_session(request: Request, response: Response, session: Session, user: AccountIdentity,
                          now: datetime) -> dict:
    settings = request.app.state.settings
    label = signed_in(request, session, user, now)
    token, row = browser_sessions.start_session(session, user.id, settings, now, user.credential_epoch, label)
    browser_sessions.set_cookie(response, token, row, settings, now)
    return {"authenticated": True, "user": _user_out(user), "expires_at": row.expires_at,
            "csrf_token": browser_sessions.csrf_token_for(token, settings)}


@auth.post("/auth/refresh", response_model=TokenPairOut,
           summary="Rotate a single-use refresh credential; replay ends its session.")
def refresh_native(payload: RefreshIn, request: Request, session: Session = Depends(get_session),
                   now: datetime = Depends(server_now)) -> dict:
    throttle(request, "refresh_ip")
    return NativeSessionService(session, request.app.state.settings, now).refresh(payload.refresh_token.get_secret_value())


@auth.post("/auth/logout", status_code=204, response_class=Response,
           summary="Revoke the refresh credential's session and its access tokens (idempotent).")
def logout_native(payload: RefreshIn, request: Request, session: Session = Depends(get_session),
                  now: datetime = Depends(server_now)) -> Response:
    throttle(request, "logout_ip")
    NativeSessionService(session, request.app.state.settings, now).logout(payload.refresh_token.get_secret_value())
    return Response(status_code=204)


class BrowserSessionOut(BaseModel):
    authenticated: bool
    user: UserOut | None = None
    #: Send back as X-CSRF-Token on every unsafe request. Keep it in memory only (never in storage or a URL).
    csrf_token: str | None = None
    expires_at: datetime | None = None


class BrowserLoginOut(BrowserSessionOut):
    #: Two-step sign-in: finish with POST /auth/browser/mfa (no cookie was set yet).
    mfa_required: bool = False
    mfa_token: str | None = None


@auth.post("/auth/browser/login", response_model=BrowserLoginOut,
           summary="Sign a browser in: sets the HttpOnly session cookie and returns the CSRF token.")
def browser_login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    user = _check_credentials(payload, session, request)
    challenge = mfa_challenge(request, session, user, now)
    if challenge is not None:
        return {"authenticated": False, **challenge}
    return start_browser_session(request, response, session, user, now)


@auth.get("/auth/browser/session", response_model=BrowserSessionOut,
          summary="The browser's session (never a 401: `authenticated` says whether it is signed in).")
def browser_session(request: Request, session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    token = request.cookies.get(browser_sessions.COOKIE_NAME)
    try:
        row = browser_sessions.live_session(session, token, now)
    except ApiError:
        return {"authenticated": False}
    user = session.get(models.User, row.user_id)
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": _user_out(user), "expires_at": row.expires_at,
            "csrf_token": browser_sessions.csrf_token_for(token, request.app.state.settings)}


@auth.post("/auth/browser/logout", status_code=204, response_class=Response,
           summary="Sign the browser out: revokes the session on the server and clears the cookie.")
def browser_logout(request: Request, session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> Response:
    settings = request.app.state.settings
    token = request.cookies.get(browser_sessions.COOKIE_NAME)
    response = Response(status_code=204)
    row = browser_sessions.find_session(session, token) if token else None
    if row is not None and row.revoked_at is None:
        browser_sessions.check_request(request, token, settings)
        row.revoked_at = now
        session.commit()
    browser_sessions.clear_cookie(response, settings)
    return response


@auth.get("/me", response_model=UserOut, summary="The authenticated account.")
@auth.get("/auth/me", response_model=UserOut, summary="The authenticated account (native authentication API).")
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


@auth.post("/me/task-data/reset", response_model=ResetTaskDataOut,
           summary="Remove all of the authenticated account's task, schedule and execution data.")
def reset_my_task_data(
    payload: ResetTaskDataIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """
    One transaction under the account's lock (backend/task_data_reset.py). The account is the token's; no
    user id is accepted from the request. The account, its credentials and its scheduling settings stay.
    """
    if not payload.confirm:
        raise ApiError(422, "validation_error", "Set confirm to true to reset all task data.")
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return reset_task_data(mutator)


# -----------------------------------------------------------------------------
# Resources
# -----------------------------------------------------------------------------


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


_PAGES: dict[type, type[BaseModel]] = {}


def page_model(out_schema: type[BaseModel]) -> type[BaseModel]:
    """A typed page of `out_schema` records (e.g. TaskOutPage), so the OpenAPI schema names every item's shape."""
    if out_schema not in _PAGES:
        _PAGES[out_schema] = create_model(
            f"{out_schema.__name__}Page",
            items=(list[out_schema], ...),
            next_cursor=(str | None, Field(default=None, description="Pass as `cursor` for the next page.")),
        )
    return _PAGES[out_schema]


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
    router.add_api_route("", list_records, methods=["GET"], response_model=page_model(out_schema),
                         operation_id=f"list_{name}")
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


@executions.get("", response_model=page_model(ExecutionOut), operation_id="list_execution")
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


#: The Day page's Uncompleted | Tasks | Completed outcome of one saved placement (backend/outcomes.py).
placement_outcomes = APIRouter(prefix="/placements", tags=["executions"])


@placement_outcomes.post("/{placement_id}/outcome", response_model=OutcomeOut, operation_id="placement_outcome")
def placement_outcome(
    placement_id: uuid.UUID,
    payload: OutcomeIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Move one of the caller's placements to pending, completed or uncompleted (its execution's actions)."""
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return set_placement_outcome(mutator, placement_id, payload)


#: Date-level views of the caller's scheduled work (backend/days.py).
days = APIRouter(prefix="/days", tags=["days"])


@days.get("/summary", operation_id="day_summaries")
def get_day_summaries(
    request: Request,
    start_date: date_ = Query(),
    end_date: date_ = Query(),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Every date's scheduled/completed/uncompleted/pending counts, minutes, points and status class."""
    return {"days": day_summaries(session, user_id, request.app.state.clock, start_date, end_date)}


@days.post("/{day}/outcome", response_model=DayOutcomeOut, operation_id="day_outcome")
def post_day_outcome(
    day: date_,
    payload: DayOutcomeIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """All of the date's scheduled tasks completed (or uncompleted, or back to pending), atomically."""
    with mutation(session, user_id, request.app.state.clock) as mutator:
        return set_day_outcome(mutator, day, payload)


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
    include_task_types: bool = Query(
        default=False,
        description="Also return task-type changes. Off by default: a client that predates task types does not "
                    "know the record type, and its cursor still advances past them.",
    ),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    size = _page_size(request, limit)
    conditions = [models.ChangeLogEntry.user_id == user_id, models.ChangeLogEntry.seq > after]
    if not include_task_types:
        conditions.append(models.ChangeLogEntry.entity_type != "task_type")
    # Each entry's snapshot, its typed row and its child rows load for the whole page (a few queries per page).
    rows = list(session.scalars(
        select(models.ChangeLogEntry).where(*conditions).order_by(models.ChangeLogEntry.seq).limit(size + 1)
    ))
    more = len(rows) > size
    rows = rows[:size]
    return {
        "changes": [
            {"seq": row.seq, "entity_type": row.entity_type, "entity_id": row.entity_id, "operation": row.operation,
             "version": row.version, "recorded_at": row.recorded_at, "record": snapshots.decode(row.revision)}
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
    app.include_router(placement_outcomes)
    app.include_router(days)
    app.include_router(changes)


__all__ = ["install_routes", "current_user_id", "throttle", "version_conflict"]
