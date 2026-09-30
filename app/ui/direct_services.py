"""
app/ui/direct_services.py

The desktop's direct-PostgreSQL storage mode (docs/direct-postgres.md):
the counterpart of app/ui/app_services.py's SQLite AppServices, with the
same surface the app uses -- planning/execution/productivity controllers, a
workspace with an epoch and a result guard, switch_workspace(), close() --
built on app/persistence's services instead of the local database.

    services = open_direct_app_services(env_file=".env", timezone=...)
    account = DirectAccountController(services)       # for the Account page

Differences from local mode, all deliberate:
    - No SQLite database is opened and no instance lock is taken; nothing is
      stored on this computer except the window's appearance settings.
    - No HTTP synchronization: PostgreSQL is the one authoritative copy, so
      there is no outbox, no association of local records and no conflict
      review (the Account page hides them).
    - Until someone signs in, every page's controller refuses with "You are
      signed out" (a SignedOutAccount); nothing is read or written.
      Signing in binds new controllers to the verified account; signing out
      rebuilds them signed-out again. Either way the workspace epoch
      advances, so a background result of the previous account is dropped.
    - Opening does no network I/O (the engine connects lazily);
      check_database() -- run in a background worker -- verifies the
      connection and the schema revision. The schema is never migrated.
    - A failed write is reported and nothing is saved anywhere else: there is
      no fallback to SQLite or memory.

Importing this module imports only app.persistence's pure modules; the
database packages load when direct mode is opened.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.persistence import errors as direct_errors
from app.persistence import load_direct_settings, open_direct_backend
from app.planning.scope import OwnerScope
from app.planning.time import validate_timezone
from app.ui.account_controller import (
    ConnectionView,
    InvalidInput,
    SignInResult,
    masked_email,
    validate_credentials,
)
from app.ui.app_services import Workspace
from app.ui.background import ControllerResult, WorkerRegistry, install_registry
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController
from app.ui.productivity_controller import ProductivityController
from config import settings

logger = logging.getLogger(__name__)

STORAGE_MODE = "postgres"
LOCATION = "PostgreSQL (direct connection)"


@dataclass
class DirectAppServices:
    backend: object
    timezone: str
    registry: WorkerRegistry
    planning_controller: PlanningController
    execution_controller: ExecutionController
    productivity_controller: ProductivityController
    workspace: Workspace
    project_root: str | None = None
    #: Safe facts about the connection (source of DATABASE_URL, TLS mode) -- never the URL.
    connection_facts: dict = field(default_factory=dict)
    account: object | None = field(default=None, repr=False)
    storage_mode: str = field(default=STORAGE_MODE, init=False)
    closed: bool = field(default=False, init=False)
    _switch_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ------------------------------------------------------------------
    # Database and accounts (call from background workers: they use the network)
    # ------------------------------------------------------------------

    def check_database(self) -> str:
        """The schema revision if the database is reachable and current; a safe DirectPersistenceError otherwise."""
        return self.backend.check_schema()

    def register(self, *, email: str, password: str, display_name: str | None = None):
        self.backend.check_schema()
        return self.backend.register(email=email, password=password, display_name=display_name)

    def sign_in(self, *, email: str, password: str):
        """Verify the credentials and bind this app to the account (call switch_workspace() on the Tk thread next)."""
        self.backend.check_schema()
        session = self.backend.sign_in(email=email, password=password)
        with self._switch_lock:
            previous, self.account = self.account, session
        if previous is not None:
            previous.sign_out()
        return session.identity

    def sign_out(self) -> None:
        """End the account's access (its controllers refuse from now on); call switch_workspace() next."""
        with self._switch_lock:
            previous, self.account = self.account, None
        if previous is not None:
            previous.sign_out()

    def profile(self):
        account = self.account
        if account is None:
            raise direct_errors.NotSignedInError()
        return account.profile()

    def reset_task_data(self) -> dict[str, int]:
        """
        Settings' "Reset All Task Data": the signed-in account's task data in
        the database, in one transaction under the account's lock -- the
        server's own implementation (backend/task_data_reset.py). This device
        keeps no copy of its own. Call from a worker; rebuild the pages after.
        """
        account = self.account
        if account is None:
            raise direct_errors.NotSignedInError()
        return account.reset_task_data()

    # ------------------------------------------------------------------
    # Workspace (the same contract as AppServices)
    # ------------------------------------------------------------------

    def switch_workspace(self, scope: OwnerScope | None = None) -> Workspace:
        """Controllers bound to the signed-in account (or signed out), with a new epoch; `scope` is informational."""
        with self._switch_lock:
            account = self.account if self.account is not None else self.backend_signed_out()
            self.planning_controller, self.execution_controller, self.productivity_controller = _build_controllers(
                account, self.timezone, self.project_root)
            self.workspace = Workspace(scope=account.scope, epoch=self.workspace.epoch + 1)
            return self.workspace

    def backend_signed_out(self):
        from app.persistence.direct import SignedOutAccount

        return SignedOutAccount(self.backend.clock)

    def workspace_guard(self) -> Callable[[], bool]:
        epoch = self.workspace.epoch
        return lambda: self.workspace.epoch == epoch and not self.closed

    def workspace_label(self) -> str:
        account = self.account
        if account is None:
            return "Signed out -- sign in on the Account page to use your schedule."
        return f"Working in {masked_email(account.identity.email)}'s account ({LOCATION})."

    def location_label(self) -> str:
        return f"Stored in {LOCATION}; needs the network. Nothing is kept on this computer."

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self, timeout: float = 10.0) -> bool:
        """Wait for background workers, then sign out and dispose the connection pool. Idempotent."""
        if self.closed:
            return True
        if not self.registry.shutdown(timeout=timeout):
            logger.warning("Waiting to close the database connection: %d background task(s) are still running.",
                           self.registry.active)
            return False
        self.sign_out()
        self.backend.close()
        self.closed = True
        return True


def _build_controllers(account, timezone_name: str, project_root: str | None
                       ) -> tuple[PlanningController, ExecutionController, ProductivityController]:
    planning = PlanningController(service=account.planning_service(), timezone=timezone_name, project_root=project_root)
    execution = ExecutionController(account.execution_service(), sync_state=lambda _execution_id: "server")
    productivity = ProductivityController(account.productivity_service(timezone_name), execution, storage="server")
    return planning, execution, productivity


def open_direct_app_services(
    *,
    env_file: str | None = None,
    timezone: str | None = None,
    project_root: str | None = None,
    registry: WorkerRegistry | None = None,
    backend=None,
    environ=None,
) -> DirectAppServices:
    """
    Direct-mode services, signed out. Reads DATABASE_URL from the environment
    and (if given) the explicitly named env file; `backend` injects a
    DirectBackend (tests). No network I/O happens here.
    """
    timezone = timezone or settings.DEFAULT_TIMEZONE
    validate_timezone(timezone)
    facts: dict = {}
    if backend is None:
        direct_settings = load_direct_settings(env_file=env_file, environ=environ)
        facts = direct_settings.describe()
        backend = open_direct_backend(direct_settings, check_schema=False)
    from app.persistence.direct import SignedOutAccount

    signed_out = SignedOutAccount(backend.clock)
    planning, execution, productivity = _build_controllers(signed_out, timezone, project_root)
    registry = registry or WorkerRegistry()
    install_registry(registry)
    services = DirectAppServices(
        backend=backend, timezone=timezone, registry=registry, planning_controller=planning,
        execution_controller=execution, productivity_controller=productivity,
        workspace=Workspace(scope=signed_out.scope, epoch=0), project_root=project_root, connection_facts=facts,
    )
    registry.result_guard = services.workspace_guard
    return services


def describe_direct_startup_failure(error: BaseException) -> str:
    """A user-facing explanation of why direct PostgreSQL storage could not be opened (never the URL)."""
    if isinstance(error, direct_errors.DirectPersistenceError):
        reason = str(error)
    elif isinstance(error, ValueError):
        reason = str(error)
    else:
        reason = f"An unexpected {type(error).__name__} occurred."
    return (
        "Direct PostgreSQL storage could not be opened, so the scheduler is not available in this session "
        "(nothing was saved, and nothing is stored on this computer instead).\n\n"
        f"{reason}\n\n"
        "Check DATABASE_URL in the environment or the env file given with --env-file, then start the app again. "
        "Run 'python -m app.app' without --storage postgres to use the local database instead."
    )


# -----------------------------------------------------------------------------
# The Account page's controller in direct mode
# -----------------------------------------------------------------------------


def friendly_direct_error(error: BaseException) -> str:
    """One safe, readable sentence (direct-mode errors are written for users; others are named, not echoed)."""
    if isinstance(error, InvalidInput):
        return "Check the highlighted fields: " + str(error)
    if isinstance(error, direct_errors.InvalidCredentialsError):
        return "The email or password is incorrect."
    if isinstance(error, direct_errors.AccountExistsError):
        return "An account with this email already exists. Sign in instead."
    if isinstance(error, direct_errors.DirectPersistenceError):
        return str(error)
    return f"Unexpected error ({type(error).__name__}). Nothing was saved."


class DirectAccountController:
    """
    What AccountPage calls, for direct PostgreSQL storage: register, sign in,
    profile, sign out and a database check. There is no backend address,
    synchronization, association or conflict review in this mode. Passwords
    are passed straight through and never kept.
    """

    storage_mode = STORAGE_MODE

    def __init__(self, services: DirectAppServices, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                 ) -> None:
        self._services = services
        self._clock = clock
        self._reachable: bool | None = None
        self._problem: str | None = None

    # -- status (no I/O: safe on the Tk thread) ----------------------------------------

    def connection(self) -> ControllerResult[ConnectionView]:
        return ControllerResult.success(self.view())

    def view(self) -> ConnectionView:
        account = self._services.account
        email = account.identity.email if account is not None else None
        tls = self._services.connection_facts.get("tls")
        where = f"{LOCATION}" + (f", TLS {tls}" if tls and tls.startswith(("require", "verify")) else "")
        if self._problem is not None:
            headline = "Database unavailable"
        elif account is not None:
            headline = f"Signed in as {masked_email(email)} · {LOCATION}"
        else:
            headline = f"Signed out · {LOCATION}"
        detail = (f"Your records are stored in {where}. Nothing is kept on this computer, and there is no offline "
                  "copy: without the database nothing can be saved.")
        if account is None:
            detail += " Sign in (or create an account) to use your schedule."
        return ConnectionView(
            state="signed_in" if account is not None else "signed_out", backend_url=None, reachable=self._reachable,
            signed_in_email=email, workspace_email=email, in_progress=False, pending=None, conflicts=0,
            last_success=None, last_success_text="not applicable (direct storage)", last_error=self._problem,
            headline=headline, detail=detail,
        )

    # -- database ------------------------------------------------------------------------

    def check_backend(self) -> ControllerResult[ConnectionView]:
        """Check the connection and the schema revision (network: run in a worker)."""
        try:
            self._services.check_database()
            self._reachable, self._problem = True, None
        except Exception as error:  # noqa: BLE001 - shown as a safe message
            self._reachable, self._problem = False, friendly_direct_error(error)
        return ControllerResult.success(self.view())

    # -- accounts ----------------------------------------------------------------------

    def register(self, email: str, password: str, *, display_name: str | None = None) -> ControllerResult[dict]:
        def op() -> dict:
            problems = validate_credentials(email, password, registering=True, display_name=display_name)
            if problems:
                raise InvalidInput(problems)
            identity = self._services.register(email=email.strip(), password=password,
                                               display_name=(display_name or "").strip() or None)
            return {"email": identity.email, "display_name": identity.display_name}

        return self._call(op)

    def sign_in(self, email: str, password: str) -> ControllerResult[SignInResult]:
        def op() -> SignInResult:
            problems = validate_credentials(email, password)
            if problems:
                raise InvalidInput(problems)
            identity = self._services.sign_in(email=email.strip(), password=password)
            return SignInResult(email=identity.email, unassociated=0, associated_before=True)

        return self._call(op)

    def sign_out(self) -> ControllerResult[ConnectionView]:
        def op() -> ConnectionView:
            self._services.sign_out()
            return self.view()

        return self._call(op, uses_database=False)

    def profile(self) -> ControllerResult[dict]:
        def op() -> dict:
            identity = self._services.profile()
            return {"email": identity.email, "display_name": identity.display_name}

        return self._call(op)

    # -- not available in direct mode ----------------------------------------------------

    def _unavailable(self, *_args, **_kwargs) -> ControllerResult:
        return ControllerResult.failure("Not available with direct PostgreSQL storage: there is no synchronization "
                                        "backend, outbox or local copy.", None)

    configure_backend = association_preview = associate = sync_now = conflicts = resolve = _unavailable

    def _call(self, operation, *, uses_database: bool = True):
        try:
            result = ControllerResult.success(operation())
        except Exception as error:  # noqa: BLE001 - every failure becomes a readable, safe message
            if isinstance(error, direct_errors.DatabaseUnavailableError | direct_errors.SchemaNotCurrentError):
                self._reachable, self._problem = False, friendly_direct_error(error)
            return ControllerResult.failure(friendly_direct_error(error), error)
        if uses_database:
            self._reachable, self._problem = True, None  # the database just answered
        return result
