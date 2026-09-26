"""
app/web/local_app.py

The local web profile (Milestone 4): a FastAPI service on a loopback address
that gives the web UI this device's SQLite store and the existing
synchronization client -- offline-first, like the desktop app, and never a
second copy of any scheduling or sync logic.

    create_local_app(LocalWebConfig(db_path=..., timezone=...))  # then serve it with uvicorn
    python -m app.web --data-dir DIR --timezone Europe/Berlin        # the launcher (app/web/__main__.py)

What it serves (docs/web-api.md, "Local profile"):

    - the same planning API as the hosted server (backend/planning_api.py's
      router) and the same record endpoints (/projects, /tasks, /fixed-blocks,
      /preferences; app/web/records.py), backed by PlanningService over SQLite;
    - /local/...: the local session, backend configuration and connectivity,
      account registration/sign-in/profile/sign-out, the explicit association
      of ownerless local data (preview, then confirm), sync status, Sync now,
      and conflicts with the actions each allows -- all delegated to the
      desktop's SyncService, whose token stays in this process's memory;
    - the production frontend's static files, when configured.

Scope: every request is scoped when it starts -- the selected account's
records if an account is selected (signed in, or signed in before and asked
to sign in again), otherwise the ownerless local workspace -- and its work
stays in that scope even if the account changes while it runs. Signing in
never claims ownerless records; creating records while signed in makes them
the account's explicitly (the association step alone claims old ones).

Security: see app/web/local_session.py (Host check, one-time bootstrap code,
session cookie, CSRF token, same-origin checks). The launcher refuses
non-loopback addresses.

Startup opens (and migrates) the database before the service accepts
requests; shutdown stops the sync loop, waits for requests in progress, and
only then closes SQLite.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.execution.db import get_connection, transaction_state_for
from app.execution.instance_lock import acquire_instance_lock
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.planning.time import validate_timezone
from app.sync.engine import AssociationError, ConflictResolutionError
from app.sync.service import SyncService, SyncStatus
from app.sync.store import BACKEND_URL_SETTING, Account, Conflict, SyncStore
from app.sync.transport import AuthenticationError, HttpTransport, ProtocolError, SyncTransport, TransportError
from app.web.local_session import LocalGuard
from app.web.records import build_records_router
from backend.api import UserOut
from backend.errors import ApiError, install_error_handlers
from backend.planning_api import (
    ENGINES,
    MAX_CSV_BYTES,
    CapabilitiesOut,
    EngineOut,
    PlanningContext,
    build_planning_router,
)
from backend.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

logger = logging.getLogger(__name__)

#: How this process names itself in the database lock (shown to a second process that is refused).
LOCK_HOLDER = "the local web service"
#: Paths that belong to the API: never answered with the frontend's index.html.
API_PREFIXES = ("planning", "local", "projects", "tasks", "fixed-blocks", "preferences", "docs", "openapi.json", "redoc")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalWebConfig:
    db_path: Path
    #: The IANA timezone this device plans in (like the desktop's SCHEDULE_MAXING_TIMEZONE).
    timezone: str = "UTC"
    host: str = "127.0.0.1"
    port: int = 8765
    #: A backend to use instead of the one saved in local settings (None: the saved one, if any).
    backend_url: str | None = None
    #: The built frontend (index.html and assets); None serves the API only.
    static_dir: Path | None = None
    bootstrap_code: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    #: Further Host header values to accept (tests use "testserver").
    extra_hosts: tuple[str, ...] = ()
    sync_interval: float = 60.0
    background_sync: bool = True
    shutdown_timeout: float = 10.0


class _Work:
    """Counts requests in progress, so shutdown can wait for them before closing SQLite."""

    def __init__(self) -> None:
        self._active = 0
        self._idle = threading.Condition()

    def __enter__(self) -> None:
        with self._idle:
            self._active += 1

    def __exit__(self, *_exc) -> None:
        with self._idle:
            self._active -= 1
            self._idle.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: self._active == 0, timeout)


class LocalRuntime:
    """This device's database and sync client, shared by every request of the local service."""

    def __init__(self, config: LocalWebConfig, transport_factory: Callable[[str], SyncTransport], clock) -> None:
        validate_timezone(config.timezone)
        self.config = config
        self.clock = clock
        self.transport_factory = transport_factory
        # One process per database: the desktop app and this service never share its sync session (instance_lock.py).
        self.instance_lock = acquire_instance_lock(config.db_path, LOCK_HOLDER)
        try:
            self.connection = get_connection(config.db_path)
        except BaseException:
            if self.instance_lock is not None:
                self.instance_lock.release()
            raise
        try:
            self.planning = PlanningService(PlanningRepository(self.connection), clock)
            self.executions = ExecutionService(ExecutionRepository(self.connection), clock)
            self.backend_error: str | None = None
            url = config.backend_url or SyncStore(self.connection).setting(BACKEND_URL_SETTING)  # before the service
            # Starting with the configured backend is not a backend switch: like the desktop app, it keeps the
            # device's active account (and so the workspace; SyncService.workspace_scope). set_backend switches.
            self.sync = SyncService(self.connection, self._transport_for(url), clock=clock,
                                    interval=config.sync_interval)
            if self.sync.configured:
                if config.backend_url is not None:
                    self.sync.remember_backend_url(url)
                if config.background_sync:
                    self.sync.start()
        except BaseException:
            self.connection.close()
            if self.instance_lock is not None:
                self.instance_lock.release()
            raise
        self.work = _Work()
        self.closed = False

    def _transport_for(self, url: str | None) -> SyncTransport | None:
        """The transport for a configured URL, or None (offline) -- an invalid URL never prevents local use."""
        if not url:
            return None
        try:
            return self.transport_factory(url)
        except ValueError as error:
            self.backend_error = str(error)
            logger.warning("Ignoring an invalid backend URL; synchronization is off.")
            return None

    def _use_backend(self, url: str | None, *, persist: bool) -> None:
        self.backend_error = None
        transport = self._transport_for(url)
        if url and transport is None:
            self.sync.set_transport(None)
            return
        self.sync.set_transport(transport)
        if persist:
            self.sync.remember_backend_url(url)
        if self.config.background_sync and not getattr(self, "closed", False):
            self.sync.start()  # a no-op without a backend or when the loop already runs

    def set_backend(self, url: str | None) -> None:
        if url:
            try:
                self.transport_factory(url)
            except ValueError as error:
                raise ApiError(422, "validation_error", str(error)) from None
        self._use_backend(url, persist=True)

    def scope(self) -> OwnerScope:
        # The same workspace rule as the desktop app (SyncService.workspace_scope).
        return self.sync.workspace_scope()

    def close(self) -> bool:
        if self.closed:
            return True
        timeout = self.config.shutdown_timeout
        stopped = self.sync.stop(timeout=timeout)
        idle = self.work.wait_idle(timeout)
        state = transaction_state_for(self.connection)
        acquired = state.lock.acquire(timeout=timeout) if state is not None else True
        if not acquired:
            # Never close SQLite under a running transaction; process exit ends it (see AppServices.close).
            logger.warning("A database transaction is still running; the database was not closed.")
            return False
        try:
            self.connection.close()
        finally:
            if state is not None:
                state.lock.release()
            if self.instance_lock is not None:
                self.instance_lock.release()
        self.closed = True
        return stopped and idle


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapIn(Strict):
    bootstrap_code: str = Field(min_length=1, max_length=200)


class AccountOut(BaseModel):
    user_id: uuid.UUID
    email: str | None
    backend_url: str
    #: Whether this device's ownerless records were associated with the account here.
    associated: bool
    signed_in: bool
    #: The account is selected but its session with the backend ended: sign in again.
    auth_required: bool


class WorkspaceOut(BaseModel):
    #: ownerless: this device's records without an account; account: the selected account's records.
    scope: Literal["ownerless", "account"]
    account: AccountOut | None
    timezone: str


class SessionOut(BaseModel):
    session: bool
    #: Send back as X-CSRF-Token on every unsafe request; keep it in memory only.
    csrf_token: str | None = None
    profile: Literal["local"] = "local"
    workspace: WorkspaceOut | None = None


class BackendIn(Strict):
    #: https://... (or http:// for a local server); null to work offline without a backend.
    backend_url: str | None = Field(default=None, max_length=2000)


class BackendStatusOut(BaseModel):
    configured: bool
    backend_url: str | None
    #: From the last request or probe: true/false, or null if not known yet.
    reachable: bool | None
    checked_at: str | None
    #: Why a configured URL is not in use (e.g. it is not a valid URL).
    error: str | None = None


class RegisterIn(Strict):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    username: str | None = Field(default=None, min_length=3, max_length=64)
    display_name: str | None = Field(default=None, max_length=200)


class SignInIn(Strict):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class SignInOut(BaseModel):
    workspace: WorkspaceOut
    #: Ownerless records on this device that the association step could claim (never claimed by signing in).
    unassociated_records: int


class ProfileIn(Strict):
    base_version: int = Field(gt=0)
    display_name: str | None = Field(default=None, max_length=200)


class AccountStateOut(BaseModel):
    workspace: WorkspaceOut
    #: The backend's profile (/me); null when signed out or the backend could not be asked.
    profile: UserOut | None = None
    profile_error: str | None = None


class AssociationPreviewOut(BaseModel):
    account: AccountOut
    #: Ownerless records per type that would become the account's (tombstones included) ...
    counts: dict[str, int]
    #: ... and how many of them are live.
    live_counts: dict[str, int]
    total: int
    #: Records that cannot be associated as they are; while any exist, confirming is refused.
    problems: list[dict[str, Any]]
    #: Confirm with exactly this token; if the local records change, preview again.
    token: str


class AssociationIn(Strict):
    confirmation: str = Field(min_length=64, max_length=64)


class AssociationOut(BaseModel):
    associated: dict[str, int]
    workspace: WorkspaceOut


class SyncStatusOut(BaseModel):
    #: The local service answered (a browser that cannot reach it gets no response at all).
    local_api: Literal["available"] = "available"
    backend: BackendStatusOut
    signed_in: bool
    auth_required: bool
    account: AccountOut | None
    in_progress: bool
    #: Records of the account waiting for the backend (each counted once); null without an account.
    pending: int | None
    conflicts: int
    last_successful_sync_at: str | None
    #: inert | ok | offline | auth_required | error
    last_status: str
    last_error: str | None


class SyncRunOut(BaseModel):
    status: str
    pushed: int
    pulled: int
    conflicts: int
    message: str
    sync: SyncStatusOut


class ConflictOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    #: push_conflict | push_rejected | pull_conflict
    kind: str
    status: str
    base_version: int | None
    local_record: dict[str, Any] | None
    #: The server's record (a tombstone when deleted there), if known.
    remote_record: dict[str, Any] | None
    remote_deleted: bool
    error: dict[str, Any] | None
    created_at: str
    resolved_at: str | None
    resolution: dict[str, Any] | None
    #: The resolutions that will work (never a merge).
    allowed_actions: list[Literal["accept_remote", "keep_local"]]
    #: Why the others are not offered.
    unavailable_actions: dict[str, str]


class ResolveIn(Strict):
    choice: Literal["accept_remote", "keep_local"]


# -----------------------------------------------------------------------------
# Conversions and errors
# -----------------------------------------------------------------------------


def _account_out(account: Account | None, status: SyncStatus) -> AccountOut | None:
    if account is None:
        return None
    return AccountOut(user_id=uuid.UUID(account.user_id), email=account.email, backend_url=account.backend_url,
                      associated=account.associated_at is not None, signed_in=status.signed_in,
                      auth_required=status.auth_required)


def _workspace(runtime: LocalRuntime) -> WorkspaceOut:
    """The workspace requests work in now (runtime.scope()): the selected account, the device's active one, or none."""
    status = runtime.sync.status()
    account = runtime.sync.workspace_account()
    if account is not None and (status.account is None or status.account.account_key != account.account_key):
        # The device's active account, not selected in this session: its records, offline until signed in.
        out = AccountOut(user_id=uuid.UUID(account.user_id), email=account.email, backend_url=account.backend_url,
                         associated=account.associated_at is not None, signed_in=False, auth_required=True)
    else:
        out = _account_out(account, status)
    return WorkspaceOut(scope="account" if out else "ownerless", account=out, timezone=runtime.config.timezone)


def _backend(runtime: LocalRuntime, status: SyncStatus) -> BackendStatusOut:
    return BackendStatusOut(configured=status.configured, backend_url=status.backend_url,
                            reachable=status.backend_reachable, checked_at=status.backend_checked_at,
                            error=runtime.backend_error)


def _sync_status(runtime: LocalRuntime) -> SyncStatusOut:
    status = runtime.sync.status()
    return SyncStatusOut(
        backend=_backend(runtime, status), signed_in=status.signed_in, auth_required=status.auth_required,
        account=_account_out(status.account, status), in_progress=status.in_progress, pending=status.pending,
        conflicts=status.conflicts, last_successful_sync_at=status.last_successful_sync_at,
        last_status=status.last_report.status, last_error=status.last_error,
    )


def _conflict_out(runtime: LocalRuntime, conflict: Conflict) -> ConflictOut:
    actions = runtime.sync.conflict_actions(conflict) if conflict.status == "open" else {}
    remote = conflict.remote_record
    return ConflictOut(
        id=conflict.id, entity_type=conflict.entity_type, entity_id=conflict.entity_id, kind=conflict.kind,
        status=conflict.status, base_version=conflict.base_version, local_record=conflict.local_record,
        remote_record=remote, remote_deleted=bool(remote and remote.get("deleted_at")), error=conflict.error,
        created_at=conflict.created_at, resolved_at=conflict.resolved_at, resolution=conflict.resolution,
        allowed_actions=[choice for choice, reason in actions.items() if reason is None],
        unavailable_actions={choice: reason for choice, reason in actions.items() if reason is not None},
    )


def _local_error(error: Exception, *, signing_in: bool = False) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, AuthenticationError):
        if signing_in:
            return ApiError(401, "invalid_credentials", "The email or password is incorrect.")
        return ApiError(401, "backend_session_expired", "The backend ended the session; sign in again.")
    if isinstance(error, TransportError):
        return ApiError(503, "backend_unreachable", str(error))
    if isinstance(error, ProtocolError):
        body = error.body or {}
        details = {key: value for key, value in body.items() if key not in ("code", "message")}
        return ApiError(error.status or 502, error.code or "backend_refused", body.get("message") or str(error), **details)
    if isinstance(error, AssociationError):
        details = {}
        if error.preview is not None:
            details = {"problems": error.preview.problems, "token": error.preview.token}
        return ApiError(409, error.code, str(error), **details)
    if isinstance(error, ConflictResolutionError):
        return ApiError(409, "resolution_refused", str(error))
    if isinstance(error, RuntimeError):
        message = str(error)
        if message.startswith("No backend"):
            return ApiError(409, "backend_not_configured", "No backend is configured on this device.")
        if message.startswith("Sign in"):
            return ApiError(401, "sign_in_required", "Sign in to an account first.")
        return ApiError(409, "account_changed", message)
    raise error


def _call(operation: Callable[[], Any], *, signing_in: bool = False) -> Any:
    try:
        return operation()
    except Exception as error:  # noqa: BLE001 - mapped to the API's structured errors
        raise _local_error(error, signing_in=signing_in) from None


# -----------------------------------------------------------------------------
# The application
# -----------------------------------------------------------------------------


def create_local_app(
    config: LocalWebConfig,
    *,
    transport_factory: Callable[[str], SyncTransport] = HttpTransport,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    runtime = LocalRuntime(config, transport_factory, clock)
    guard = LocalGuard(port=config.port, bootstrap_code=config.bootstrap_code, extra_hosts=config.extra_hosts)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="Schedule Maxing (local)", version="1.0.0", lifespan=lifespan,
                  description="This device's planning data and synchronization, for the web UI. Loopback only.")
    app.state.runtime = runtime
    app.state.guard = guard
    app.state.clock = clock
    install_error_handlers(app)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        try:
            guard.check_host(request)
        except ApiError as error:
            return JSONResponse(error.body(), status_code=error.status)
        if runtime.closed:
            return JSONResponse({"error": {"code": "shutting_down", "message": "The local service is stopping."}},
                                status_code=503)
        with runtime.work:
            return await call_next(request)

    def require_session(request: Request) -> None:
        guard.require(request)

    def local_context(request: Request) -> PlanningContext:
        guard.require(request)
        # The scope is fixed here, for the whole request: an account switch meanwhile cannot redirect its work.
        return PlanningContext(service=runtime.planning.scoped(runtime.scope()), profile="local")

    def local_capabilities(_request: Request) -> CapabilitiesOut:
        status = runtime.sync.status()
        return CapabilitiesOut(
            profile="local", persistence="device", reports_device_pending_changes=True,
            engines=[EngineOut(mode=mode, description=text) for mode, text in ENGINES.items()],
            generation_modes=["full", "incremental"], max_range_days=62, csv_format_version=2,
            max_csv_bytes=MAX_CSV_BYTES,
            auth={"local_session": True, "browser_sessions": False, "bearer_tokens": False,
                  "registration": status.configured},
            extra={"timezone": config.timezone, "sync_configured": status.configured,
                   "note": "Changes are saved on this device first and reach the backend when it synchronizes."},
        )

    app.include_router(build_planning_router(local_context, local_capabilities))
    app.include_router(build_records_router(local_context))
    app.include_router(_local_router(runtime, guard, require_session))
    if config.static_dir is not None:
        _install_frontend(app, Path(config.static_dir))
    return app


def _local_router(runtime: LocalRuntime, guard: LocalGuard, require_session) -> APIRouter:
    router = APIRouter(prefix="/local", tags=["local"])
    protected = [Depends(require_session)]

    @router.get("/session", response_model=SessionOut, operation_id="local_session",
                summary="Whether this browser has a local session (never a 401).")
    def get_session(request: Request) -> SessionOut:
        token = guard.session_token(request)
        if token is None:
            return SessionOut(session=False)
        return SessionOut(session=True, csrf_token=guard.csrf_token(token), workspace=_workspace(runtime))

    @router.post("/session", response_model=SessionOut, operation_id="local_session_start",
                 summary="Exchange the launcher's one-time bootstrap code for the local session cookie.")
    def start_session(body: BootstrapIn, request: Request, response: Response) -> SessionOut:
        guard.check_unsafe_origin(request)
        csrf = guard.exchange(body.bootstrap_code, response)
        return SessionOut(session=True, csrf_token=csrf, workspace=_workspace(runtime))

    @router.delete("/session", status_code=204, response_class=Response, dependencies=protected,
                   operation_id="local_session_end")
    def end_session(request: Request) -> Response:
        response = Response(status_code=204)
        guard.end(request, response)
        return response

    @router.get("/backend", response_model=BackendStatusOut, dependencies=protected, operation_id="local_backend")
    def get_backend() -> BackendStatusOut:
        return _backend(runtime, runtime.sync.status())

    @router.put("/backend", response_model=BackendStatusOut, dependencies=protected, operation_id="local_backend_set",
                summary="Use another backend (or none). The current account session ends first; no data changes.")
    def set_backend(body: BackendIn) -> BackendStatusOut:
        _call(lambda: runtime.set_backend(body.backend_url.strip() if body.backend_url else None))
        return _backend(runtime, runtime.sync.status())

    @router.post("/backend/check", response_model=BackendStatusOut, dependencies=protected,
                 operation_id="local_backend_check", summary="Probe whether the backend answers.")
    def check_backend() -> BackendStatusOut:
        _call(runtime.sync.check_connectivity)
        return _backend(runtime, runtime.sync.status())

    @router.post("/account/register", response_model=UserOut, status_code=201, dependencies=protected,
                 operation_id="local_register", summary="Create an account on the backend (does not sign in).")
    def register(body: RegisterIn) -> dict:
        return _call(lambda: runtime.sync.register(body.email, body.password, username=body.username,
                                                   display_name=body.display_name))

    @router.post("/account/sign-in", response_model=SignInOut, dependencies=protected, operation_id="local_sign_in",
                 summary="Sign in. Never claims or uploads this device's ownerless records.")
    def sign_in(body: SignInIn) -> SignInOut:
        _call(lambda: runtime.sync.sign_in(body.email, body.password), signing_in=True)
        preview = _call(runtime.sync.association_preview)
        return SignInOut(workspace=_workspace(runtime), unassociated_records=preview.total)

    @router.get("/account", response_model=AccountStateOut, dependencies=protected, operation_id="local_account")
    def get_account() -> AccountStateOut:
        workspace = _workspace(runtime)
        if workspace.account is None or not workspace.account.signed_in:
            return AccountStateOut(workspace=workspace)
        try:
            return AccountStateOut(workspace=_workspace(runtime), profile=runtime.sync.profile())
        except Exception as error:  # noqa: BLE001 - the local data stays usable; say why the profile is missing
            return AccountStateOut(workspace=_workspace(runtime), profile_error=_local_error(error).message)

    @router.patch("/account/profile", response_model=UserOut, dependencies=protected, operation_id="local_profile_update")
    def update_profile(body: ProfileIn) -> dict:
        return _call(lambda: runtime.sync.update_profile(body.base_version, body.display_name))

    @router.post("/account/sign-out", response_model=WorkspaceOut, dependencies=protected, operation_id="local_sign_out",
                 summary="Sign out: the token is dropped; local records (and pending changes) stay on this device.")
    def sign_out() -> WorkspaceOut:
        runtime.sync.sign_out()
        return _workspace(runtime)

    @router.get("/association/preview", response_model=AssociationPreviewOut, dependencies=protected,
                operation_id="local_association_preview",
                summary="What associating this device's ownerless records would claim. Writes nothing.")
    def association_preview() -> AssociationPreviewOut:
        preview = _call(runtime.sync.association_preview)
        status = runtime.sync.status()
        return AssociationPreviewOut(account=_account_out(status.account, status), counts=preview.counts,
                                     live_counts=preview.live_counts, total=preview.total, problems=preview.problems,
                                     token=preview.token)

    @router.post("/association", response_model=AssociationOut, dependencies=protected, operation_id="local_associate",
                 summary="Associate exactly the previewed ownerless records with the signed-in account.")
    def associate(body: AssociationIn) -> AssociationOut:
        counts = _call(lambda: runtime.sync.associate_local_data(body.confirmation))
        runtime.sync.wake()
        return AssociationOut(associated=counts, workspace=_workspace(runtime))

    @router.get("/sync/status", response_model=SyncStatusOut, dependencies=protected, operation_id="local_sync_status")
    def sync_status() -> SyncStatusOut:
        return _sync_status(runtime)

    @router.post("/sync", response_model=SyncRunOut, dependencies=protected, operation_id="local_sync_now",
                 summary="Synchronize now (push, then pull) through the existing sync client.")
    def sync_now() -> SyncRunOut:
        if runtime.sync.status().in_progress:
            raise ApiError(409, "sync_in_progress", "A synchronization is already running.")
        report = runtime.sync.sync_now()
        return SyncRunOut(status=report.status, pushed=report.pushed, pulled=report.pulled,
                          conflicts=report.conflicts, message=report.message, sync=_sync_status(runtime))

    @router.get("/conflicts", response_model=list[ConflictOut], dependencies=protected, operation_id="local_conflicts")
    def list_conflicts(status: Literal["open", "resolved", "all"] = Query(default="open")) -> list[ConflictOut]:
        conflicts = runtime.sync.list_conflicts(None if status == "all" else status)
        return [_conflict_out(runtime, conflict) for conflict in conflicts]

    @router.get("/conflicts/{conflict_id}", response_model=ConflictOut, dependencies=protected,
                operation_id="local_conflict")
    def get_conflict(conflict_id: str) -> ConflictOut:
        conflict = runtime.sync.get_conflict(conflict_id)
        if conflict is None:
            raise ApiError(404, "not_found", "No such conflict for this account.")
        return _conflict_out(runtime, conflict)

    @router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictOut, dependencies=protected,
                 operation_id="local_conflict_resolve", summary="Resolve with an allowed action.")
    def resolve_conflict(conflict_id: str, body: ResolveIn) -> ConflictOut:
        return _conflict_out(runtime, _call(lambda: runtime.sync.resolve_conflict(conflict_id, body.choice)))

    return router


def _install_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built frontend: files as they are, other non-API paths as index.html (SPA deep links)."""
    root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        first = full_path.split("/", 1)[0]
        if first in API_PREFIXES:
            raise ApiError(404, "not_found", "No such API endpoint.")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        if "." in full_path.rsplit("/", 1)[-1]:
            raise ApiError(404, "not_found", "No such file.")
        index = root / "index.html"
        if not index.is_file():
            raise ApiError(404, "not_found", "The frontend is not built.")
        return FileResponse(index)
