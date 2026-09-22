"""
app/ui/app_services.py

Startup and shutdown of the desktop application's persistence stack, kept
Tk-free so it is tested headlessly (tests/ui/test_app_services.py).

open_app_services opens the one shared SQLite connection (app.execution.db:
default per-user location, legacy adoption, migrations, integrity checks)
and builds every controller on it: planning (PlanningService),
execution (ExecutionService), and productivity (ProductivityService). If
anything fails, the connection is closed and the error propagates -- the
desktop app then shows a clear error instead of the scheduler, because
there is no longer an in-memory fallback whose edits would silently never
be saved.

AppServices.close() is the orderly shutdown: it stops accepting new
background work, waits for running workers (app.ui.background) to finish,
takes the connection's lock so no transaction is mid-flight, and only then
closes the connection. It is idempotent.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.execution.db import get_connection, resolve_db_path, transaction_state_for
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from app.planning.time import validate_timezone
from app.productivity.reporting import ProductivityService
from app.ui.background import WorkerRegistry, install_registry
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController
from app.ui.productivity_controller import ProductivityController
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class AppServices:
    db_path: Path
    timezone: str
    connection: sqlite3.Connection
    planning_controller: PlanningController
    execution_controller: ExecutionController
    productivity_controller: ProductivityController
    registry: WorkerRegistry
    closed: bool = field(default=False, init=False)

    def close(self, timeout: float = 10.0) -> bool:
        """Wait for background workers, then close the connection. True if every worker finished in time."""
        if self.closed:
            return True
        finished = self.registry.shutdown(timeout=timeout)
        if not finished:
            logger.warning("Closing the database while %d background task(s) are still running.", self.registry.active)

        state = transaction_state_for(self.connection)
        acquired = state.lock.acquire(timeout=timeout) if state is not None else False
        try:
            self.connection.close()
        finally:
            if acquired:
                state.lock.release()
        self.closed = True
        return finished


def open_app_services(
    db_path: str | Path | None = None,
    *,
    timezone: str | None = None,
    project_root: str | None = None,
    registry: WorkerRegistry | None = None,
) -> AppServices:
    """
    Open the application database and build the shared controllers.

    Raises (after closing anything it opened) if the database cannot be
    opened/migrated or the timezone is invalid. The registry becomes the
    default for app.ui.background.run_in_background.
    """
    timezone = timezone or settings.DEFAULT_TIMEZONE
    validate_timezone(timezone)
    resolved = resolve_db_path(db_path)

    connection = get_connection(db_path)
    try:
        planning_controller = PlanningController(
            service=PlanningService(PlanningRepository(connection)), timezone=timezone, project_root=project_root
        )
        execution_repository = ExecutionRepository(connection)
        execution_controller = ExecutionController(ExecutionService(execution_repository))
        productivity_controller = ProductivityController(ProductivityService(execution_repository), execution_controller)
    except BaseException:
        connection.close()
        raise

    registry = registry or WorkerRegistry()
    install_registry(registry)
    return AppServices(
        db_path=resolved,
        timezone=timezone,
        connection=connection,
        planning_controller=planning_controller,
        execution_controller=execution_controller,
        productivity_controller=productivity_controller,
        registry=registry,
    )


def describe_startup_failure(error: BaseException, db_path: str | Path | None = None) -> str:
    """A user-facing explanation of why the database could not be opened."""
    location = resolve_db_path(db_path)
    return (
        "The schedule database could not be opened, so the scheduler is not available in this "
        "session (nothing you do could be saved).\n\n"
        f"Database: {location}\n"
        f"Error: {type(error).__name__}: {error}\n\n"
        f"Check that the folder is writable, or set {settings.DATA_DIR_ENV_VAR} to another folder "
        f"(current data directory: {os.fspath(settings.DATA_DIR)})."
    )
