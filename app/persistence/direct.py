"""
app/persistence/direct.py

The direct desktop-to-PostgreSQL composition: the same schema
(backend/models.py), account rules (backend/accounts.py), planning
repository (backend/planning_repository.py) and mutation path
(backend/mutations.py) the HTTP API uses -- called in-process, with no HTTP
framework, no JWT and no server.

    backend = DirectBackend.open(settings)          # one Engine per process; checks the schema revision
    backend.register(email=..., password=...)       # -> AccountIdentity (no password, no hash)
    account = backend.sign_in(email=..., password=...)
    planning = account.planning_service()           # app.planning.application.PlanningService
    executions = account.execution_service()        # the ExecutionService API, server rules
    productivity = account.productivity_service()   # app.productivity.reporting.ProductivityService
    account.sign_out(); backend.close()             # after background workers finished

Units of work: every logical operation opens its own Session from the one
shared sessionmaker, runs in one transaction (writes: backend.mutations'
per-user lock, server versions and change log, so API clients see direct
writes in their feed), and rolls back and closes the session when it ends
-- also after a read or an error. No Session is shared between threads or
kept open between user actions. Nested operations join the enclosing
transaction as savepoints (DirectPlanningRepository.transaction). Callers
receive detached domain objects (pydantic models, dataclasses), never ORM
rows.

Scope: an AccountSession exists only through sign_in(); it binds every
service to the verified account (OwnerScope.account), so a caller cannot
reach another user's records by passing a UUID. After sign_out() every
service of that session refuses to run (NotSignedInError).

Schema: open() checks that the database is at the migrations' head revision
and refuses to run otherwise (SchemaNotCurrentError). It never migrates;
schema upgrades are the explicit `python -m backend.migrate` command.

Errors reaching callers are the safe types of app/persistence/errors.py (or
the planning/execution domain errors); driver messages, URLs and SQL
parameters are never shown or logged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import Engine, create_engine
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session

from app.execution.errors import ExecutionError
from app.persistence import errors
from app.persistence.config import DirectDatabaseSettings, engine_options
from app.planning.errors import InvalidEntityError, PlanningError
from app.planning.scope import OwnerScope
from backend import accounts
from backend.database import session_factory
from backend.errors import ApiError
from backend.migrate import current_revision, head_revision

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
_CAPABILITY = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_database_error(error: BaseException) -> errors.DirectStorageError:
    """A user-safe error for a SQLAlchemy/driver failure (the original message is never copied)."""
    if isinstance(error, sa_exc.IntegrityError):
        return errors.StorageConflictError(
            "The change conflicts with data saved meanwhile; nothing was saved. Reload and try again.")
    if isinstance(error, sa_exc.TimeoutError):
        return errors.DatabaseUnavailableError(
            "The database did not answer in time (all connections are busy). Try again in a moment.")
    if isinstance(error, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.DisconnectionError)):
        state = getattr(getattr(error, "orig", None), "sqlstate", None)
        if state in ("28P01", "28000"):
            return errors.DatabaseUnavailableError("The database refused the credentials in DATABASE_URL.")
        if state == "3D000":
            return errors.DatabaseUnavailableError("The database named in DATABASE_URL does not exist.")
        if state == "57014":
            return errors.DatabaseUnavailableError("The database cancelled an operation that took too long.")
        return errors.DatabaseUnavailableError(
            "The database server could not be reached. Check the network connection, the server's inbound IP "
            "allowlist, TLS settings and DATABASE_URL, then try again.")
    return errors.DirectStorageError("The database could not complete the operation; nothing of it was saved.")


def planning_error(error: ApiError) -> Exception:
    """A domain error for an ApiError raised under the planning repository (messages are the server's own)."""
    if error.status == 401:
        return errors.NotSignedInError()
    if error.status == 409:
        return errors.StorageConflictError(error.message)
    return InvalidEntityError(error.message)


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except sa_exc.SQLAlchemyError:
        pass  # the connection is gone; closing the session releases it


class DirectBackend:
    """One Engine and sessionmaker for the process (thread-safe); a Session per logical operation."""

    def __init__(self, engine: Engine, *, clock: Clock = _utcnow) -> None:
        self._engine = engine
        self._factory = session_factory(engine)
        self._clock = clock
        self._closed = False

    @classmethod
    def open(cls, settings: DirectDatabaseSettings, *, clock: Clock = _utcnow, check_schema: bool = True
             ) -> "DirectBackend":
        """Connect with `settings` (TLS enforced for a remote server) and check the schema revision."""
        engine = create_engine(settings.effective_url(), **engine_options(settings))
        backend = cls(engine, clock=clock)
        if check_schema:
            try:
                backend.check_schema()
            except BaseException:
                engine.dispose()
                raise
        return backend

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def clock(self) -> Clock:
        return self._clock

    def schema_revision(self) -> tuple[str | None, str]:
        """(the database's revision, the revision this application needs)."""
        try:
            with self._engine.connect() as connection:
                current = current_revision(connection)
        except sa_exc.SQLAlchemyError as error:
            logger.warning("Direct database schema check failed (%s).", type(error).__name__)
            raise safe_database_error(error) from None
        return current, head_revision()

    def check_schema(self) -> str:
        """The current revision; SchemaNotCurrentError unless it is the head (never migrates)."""
        current, head = self.schema_revision()
        if current != head:
            raise errors.SchemaNotCurrentError(current, head)
        return current

    @contextmanager
    def operation(self, translate: Callable[[ApiError], Exception] = planning_error) -> Iterator[Session]:
        """
        One unit of work: a new Session, rolled back (a no-op after a commit)
        and closed when the block ends, with every database failure turned
        into a safe error.
        """
        if self._closed:
            raise errors.DirectStorageError("The database connection was closed.")
        session = None
        try:
            session = self._factory()  # inside the guard: even creating it may fail
            yield session
        except (errors.DirectPersistenceError, PlanningError, ExecutionError, accounts.AccountError):
            raise
        except ApiError as error:
            raise translate(error) from None
        except sa_exc.SQLAlchemyError as error:
            logger.warning("A direct database operation failed (%s).", type(error).__name__)
            raise safe_database_error(error) from None
        finally:
            if session is not None:
                _rollback_quietly(session)  # ends any transaction (a no-op after a commit)
                try:
                    session.close()
                except sa_exc.SQLAlchemyError:
                    pass  # the connection is gone; the pool discards it

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def register(self, *, email: str, password: str, username: str | None = None,
                 display_name: str | None = None) -> accounts.AccountIdentity:
        """Create an account (the shared rules of backend/accounts.py). Does not sign in."""
        with self._account_errors(), self.operation() as session:
            return accounts.AccountService(session, self._clock).register(
                email=email, password=password, username=username, display_name=display_name)

    def sign_in(self, *, password: str, email: str | None = None, username: str | None = None) -> "AccountSession":
        """An AccountSession bound to the verified account; InvalidCredentialsError otherwise."""
        with self._account_errors(), self.operation() as session:
            identity = accounts.AccountService(session, self._clock).authenticate(
                email=email, username=username, password=password)
        return AccountSession(self, identity, _capability=_CAPABILITY)

    def account_exists(self, *, email: str) -> bool:
        """Whether an account uses this (normalized) email -- for operator tools, never for a sign-in screen."""
        from sqlalchemy import select

        from backend import models
        from backend.passwords import normalize_identifier

        with self.operation() as session:
            return session.scalar(select(models.User.id).where(
                models.User.email == normalize_identifier(email))) is not None

    @contextmanager
    def _account_errors(self) -> Iterator[None]:
        try:
            yield
        except accounts.InvalidCredentialsError as error:
            raise errors.InvalidCredentialsError(str(error)) from None
        except accounts.AccountExistsError as error:
            raise errors.AccountExistsError(str(error)) from None
        except accounts.AccountValidationError as error:
            raise errors.AccountValidationError(str(error)) from None

    def close(self) -> None:
        """Dispose the connection pool (call after every worker using it finished). Idempotent."""
        if not self._closed:
            self._closed = True
            self._engine.dispose()


class _Services:
    """The services of an account (or of the signed-out state), built on its units of work."""

    def planning_service(self):
        from app.persistence.planning import DirectPlanningRepository
        from app.planning.application import PlanningService

        return PlanningService(DirectPlanningRepository(self), self.clock)

    def execution_service(self):
        from app.persistence.executions import DirectExecutionService

        return DirectExecutionService(self)

    def productivity_service(self, timezone_name: str | None = None):
        """Productivity analysis of the account; the schedule cohort reads its planning history (one session)."""
        from app.persistence.executions import DirectExecutionReader
        from app.productivity.reporting import ProductivityService

        return ProductivityService(DirectExecutionReader(self), history=self.planning_service(),
                                   timezone_name=timezone_name)


class AccountSession(_Services):
    """
    A signed-in account of a DirectBackend. Created only by
    DirectBackend.sign_in(); every service it hands out works in this
    account's scope and stops working after sign_out().
    """

    def __init__(self, backend: DirectBackend, identity: accounts.AccountIdentity, *, _capability: object) -> None:
        if _capability is not _CAPABILITY:
            raise TypeError("An AccountSession is created by DirectBackend.sign_in(), never directly.")
        self._backend = backend
        self._identity = identity
        self._active = True

    @property
    def identity(self) -> accounts.AccountIdentity:
        return self._identity

    @property
    def user_id(self) -> uuid.UUID:
        return self._identity.id

    @property
    def scope(self) -> OwnerScope:
        return OwnerScope.account(self._identity.id)

    @property
    def active(self) -> bool:
        return self._active and not self._backend.closed

    @property
    def clock(self) -> Clock:
        return self._backend.clock

    def require_active(self) -> None:
        if not self.active:
            raise errors.NotSignedInError()

    def operation(self, translate: Callable[[ApiError], Exception] = planning_error):
        self.require_active()
        return self._backend.operation(translate)

    def reset_task_data(self) -> dict[str, int]:
        """
        Remove this account's task, schedule and execution data in one
        transaction under its lock -- the server's own implementation
        (backend/task_data_reset.py); the account and its settings stay.
        Returns how many live records of each type were removed.
        """
        from backend.mutations import mutation
        from backend.task_data_reset import reset_task_data

        with self.operation() as session:
            with mutation(session, self.user_id, self.clock) as mutator:
                return reset_task_data(mutator)["removed"]

    def profile(self) -> accounts.AccountIdentity:
        """The account as stored now (display name, version), re-read."""
        with self.operation() as session:
            identity = accounts.AccountService(session).get(self.user_id)
        if identity is None:
            self._active = False
            raise errors.NotSignedInError()
        self._identity = identity
        return identity

    def sign_out(self) -> None:
        """End the session: every service of it refuses further work. Idempotent."""
        self._active = False


class SignedOutAccount(_Services):
    """
    Stands in for an AccountSession before anyone signed in (or after sign-out):
    its services exist, so the desktop can build its pages, but every call
    refuses with NotSignedInError and touches no database.
    """

    identity = None
    user_id = None
    active = False

    def __init__(self, clock: Clock = _utcnow) -> None:
        self._clock = clock

    @property
    def scope(self) -> OwnerScope:
        return OwnerScope.ownerless()

    @property
    def clock(self) -> Clock:
        return self._clock

    def require_active(self) -> None:
        raise errors.NotSignedInError()

    def operation(self, translate: Callable[[ApiError], Exception] = planning_error):
        raise errors.NotSignedInError()

    def sign_out(self) -> None:
        pass
