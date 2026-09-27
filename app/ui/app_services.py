"""
app/ui/app_services.py

Startup and shutdown of the desktop application's persistence stack, kept
Tk-free so it is tested headlessly (tests/ui/test_app_services.py).

open_app_services takes the database's single-process lock
(app/execution/instance_lock.py: the desktop app and the local web service
never share one database file at the same time), opens the one shared
SQLite connection (app.execution.db: default per-user location, legacy
adoption, migrations, integrity checks) and builds every controller on it:
planning (PlanningService), execution (ExecutionService), and productivity
(ProductivityService). If anything fails, everything opened is closed and
the error propagates -- the desktop app then shows a clear error instead of
the scheduler, because there is no longer an in-memory fallback whose edits
would silently never be saved.

Nothing web-related happens here: no HTTP server, listening socket, browser
session, cloud database or backend module is imported or started
(docs/desktop-web-boundaries.md; tests/test_desktop_isolation.py).

Workspace (docs/desktop-web-boundaries.md): every controller works in one
explicit owner scope, chosen by SyncService.workspace_scope() -- the
selected account, else the account active on this device, else the
ownerless local workspace -- so records of different owners are never
mixed in one view, generation, export or reset. switch_workspace() builds a
fresh set of controllers for another scope and advances the workspace
epoch: work already running keeps the scope it started in, and a result
guarded by workspace_guard() is dropped instead of reaching the new
workspace's widgets. Signing in never claims ownerless records; that stays
SyncService.associate_local_data(confirmation=...).

Synchronization (app/sync): a SyncService is always created. It is inert
unless SCHEDULE_MAXING_BACKEND_URL (or backend_url=) names a backend and an
account signs in through its service API. When a backend is configured its
background loop is started; it never holds a SQLite transaction across a
network call, and a failing backend never blocks local work.

AppServices.close() is the orderly shutdown: it stops the sync loop (waiting
for a running sync), stops accepting new background work, waits for running
workers (app.ui.background) to finish, takes the connection's lock so no
transaction is mid-flight, and only then closes the connection and releases
the database lock. If a transaction is still running when the timeout ends,
the connection is left open rather than closed under it (close() returns
False and can be called again; process exit ends it). It is idempotent.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.execution.db import get_connection, resolve_db_path, transaction_state_for
from app.execution.instance_lock import DatabaseInUseError, InstanceLock, acquire_instance_lock
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.planning.time import validate_timezone
from app.productivity.reporting import ProductivityService
from app.sync.service import SyncService
from app.sync.store import BACKEND_URL_SETTING, SyncStore
from app.sync.transport import HttpTransport, SyncTransport
from app.ui.background import WorkerRegistry, install_registry
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController
from app.ui.productivity_controller import ProductivityController
from config import settings

logger = logging.getLogger(__name__)

#: How this process names itself in the database lock (shown to a second process that is refused).
LOCK_HOLDER = "the desktop app"


@dataclass(frozen=True)
class Workspace:
    """The owner scope the desktop's controllers currently work in."""

    scope: OwnerScope
    #: Advances on every switch_workspace(); a result started under an older epoch is stale.
    epoch: int


@dataclass
class AppServices:
    db_path: Path
    timezone: str
    connection: sqlite3.Connection
    planning_controller: PlanningController
    execution_controller: ExecutionController
    productivity_controller: ProductivityController
    registry: WorkerRegistry
    sync_service: SyncService
    workspace: Workspace
    #: The device-wide services the scoped controllers are built from (never handed to widgets).
    planning_service: PlanningService = field(repr=False)
    execution_repository: ExecutionRepository = field(repr=False)
    project_root: str | None = None
    instance_lock: InstanceLock | None = field(default=None, repr=False)
    #: "local" (this SQLite database); app/ui/direct_services.DirectAppServices is "postgres".
    storage_mode: str = field(default="local", init=False)
    closed: bool = field(default=False, init=False)
    _switch_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    def switch_workspace(self, scope: OwnerScope | None = None) -> Workspace:
        """
        Work in `scope` from now on (default: SyncService.workspace_scope(),
        e.g. after signing in or out). Builds new controllers bound to it --
        the previous ones keep their scope, so work already running with them
        finishes where it started -- and advances the epoch so that guarded
        results of earlier work are dropped. Callers re-read their views from
        the new controllers.
        """
        with self._switch_lock:
            scope = scope or self.sync_service.workspace_scope()
            planning, execution, productivity = _build_controllers(
                self.planning_service, self.execution_repository, scope, self.timezone, self.project_root
            )
            self.planning_controller, self.execution_controller, self.productivity_controller = (
                planning, execution, productivity
            )
            self.workspace = Workspace(scope=scope, epoch=self.workspace.epoch + 1)
            return self.workspace

    def workspace_guard(self) -> Callable[[], bool]:
        """For run_in_background(still_current=...): true while the workspace is the one current now."""
        epoch = self.workspace.epoch
        return lambda: self.workspace.epoch == epoch and not self.closed

    def workspace_label(self) -> str:
        return f"Working in {self.workspace.scope.describe()}."

    def location_label(self) -> str:
        return f"Saved locally in {self.db_path.name}; works offline."

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self, timeout: float = 10.0) -> bool:
        """
        Wait for the sync loop and background workers, then close the
        connection and release the database lock. True if everything
        finished in time and the connection is closed.
        """
        if self.closed:
            return True
        sync_stopped = self.sync_service.stop(timeout=timeout)
        finished = self.registry.shutdown(timeout=timeout) and sync_stopped
        if not finished:
            logger.warning("Waiting to close the database: %d background task(s) are still running.", self.registry.active)
            return False  # a waiting network worker may still need SQLite after it receives its response

        state = transaction_state_for(self.connection)
        acquired = state.lock.acquire(timeout=timeout) if state is not None else True
        if not acquired:
            # A transaction is still running: closing under it would interrupt it. Leave the connection open (the
            # transaction commits or rolls back on its own; process exit closes the file) and say so.
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
        return finished


def _sync_transport(backend_url: str | None, factory: Callable[[str], SyncTransport] = HttpTransport
                    ) -> SyncTransport | None:
    """The sync transport, or None (inert) -- a bad backend setting never prevents offline use."""
    if not backend_url:
        return None
    try:
        return factory(backend_url)
    except ValueError:
        logger.warning("Ignoring an invalid SCHEDULE_MAXING_BACKEND_URL; synchronization is off.")
        return None


def _build_controllers(
    planning_service: PlanningService,
    execution_repository: ExecutionRepository,
    scope: OwnerScope,
    timezone: str,
    project_root: str | None,
) -> tuple[PlanningController, ExecutionController, ProductivityController]:
    planning = PlanningController(service=planning_service.scoped(scope), timezone=timezone, project_root=project_root)
    executions = execution_repository.scoped(scope)
    execution = ExecutionController(ExecutionService(executions))
    productivity = ProductivityController(ProductivityService(executions), execution)
    return planning, execution, productivity


def open_app_services(
    db_path: str | Path | None = None,
    *,
    timezone: str | None = None,
    project_root: str | None = None,
    registry: WorkerRegistry | None = None,
    backend_url: str | None = None,
    background_sync: bool = True,
    transport_factory: Callable[[str], SyncTransport] = HttpTransport,
) -> AppServices:
    """
    Open the application database and build the shared controllers.

    Raises (after closing anything it opened) if the database is in use by
    another Schedule Maxing process (DatabaseInUseError), cannot be
    opened/migrated, or the timezone is invalid. The registry becomes the
    default for app.ui.background.run_in_background.
    """
    timezone = timezone or settings.DEFAULT_TIMEZONE
    validate_timezone(timezone)
    resolved = resolve_db_path(db_path)

    lock = acquire_instance_lock(resolved, LOCK_HOLDER)
    connection = None
    try:
        connection = get_connection(db_path)
        planning_service = PlanningService(PlanningRepository(connection))
        execution_repository = ExecutionRepository(connection)
        # An explicit URL, else SCHEDULE_MAXING_BACKEND_URL, else the one saved on the Account page.
        backend_url = backend_url or settings.BACKEND_URL or SyncStore(connection).setting(BACKEND_URL_SETTING)
        sync_service = SyncService(connection, _sync_transport(backend_url, transport_factory))
        scope = sync_service.workspace_scope()
        planning_controller, execution_controller, productivity_controller = _build_controllers(
            planning_service, execution_repository, scope, timezone, project_root
        )
    except BaseException:
        if connection is not None:
            connection.close()
        if lock is not None:
            lock.release()
        raise

    registry = registry or WorkerRegistry()
    install_registry(registry)
    services = AppServices(
        db_path=resolved,
        timezone=timezone,
        connection=connection,
        planning_controller=planning_controller,
        execution_controller=execution_controller,
        productivity_controller=productivity_controller,
        registry=registry,
        sync_service=sync_service,
        workspace=Workspace(scope=scope, epoch=0),
        planning_service=planning_service,
        execution_repository=execution_repository,
        project_root=project_root,
        instance_lock=lock,
    )
    registry.result_guard = services.workspace_guard  # background results never cross a workspace switch
    if background_sync:
        services.sync_service.start()  # a no-op without a configured backend
    return services


def describe_startup_failure(error: BaseException, db_path: str | Path | None = None) -> str:
    """A user-facing explanation of why the database could not be opened."""
    location = resolve_db_path(db_path)
    if isinstance(error, DatabaseInUseError):
        return (
            "The schedule database is already open in another Schedule Maxing process, so this window cannot "
            f"use it.\n\n{error}\n\nClose the other window or stop the local web service (Ctrl+C), then start "
            "the desktop app again."
        )
    return (
        "The schedule database could not be opened, so the scheduler is not available in this "
        "session (nothing you do could be saved).\n\n"
        f"Database: {location}\n"
        f"Error: {type(error).__name__}: {error}\n\n"
        f"Check that the folder is writable, or set {settings.DATA_DIR_ENV_VAR} to another folder "
        f"(current data directory: {os.fspath(settings.DATA_DIR)})."
    )
