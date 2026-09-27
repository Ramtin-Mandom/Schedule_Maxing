"""
app/persistence/errors.py

Errors of the direct PostgreSQL path, safe to show a user: every message is
written here, never copied from a driver exception, so none contains a
database URL, host credentials, SQL parameters or a password.

Storage errors subclass both PlanningError and ExecutionError, so the
existing desktop controllers (which report those as ordinary failures)
report them the same way. This module imports nothing optional, so any
layer may catch these types.
"""

from __future__ import annotations

from app.execution.errors import ExecutionError
from app.planning.errors import PlanningError


class DirectPersistenceError(Exception):
    """Base class of every direct-mode failure."""


class DirectConfigError(DirectPersistenceError):
    """The direct-database configuration is missing or unsafe (names settings, never values)."""


class DirectModeUnavailableError(DirectPersistenceError):
    """The optional direct-PostgreSQL packages are not installed."""

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"Direct PostgreSQL mode needs the optional package '{missing}'. Install it with: "
            "python -m pip install -r requirements-direct.txt"
        )


class DirectStorageError(DirectPersistenceError, PlanningError, ExecutionError):
    """The database could not complete an operation (nothing of it was saved)."""


class DatabaseUnavailableError(DirectStorageError):
    """The database could not be reached, refused the credentials, or timed out."""


class SchemaNotCurrentError(DirectStorageError):
    """The database schema is not at the revision this application needs; it is never migrated implicitly."""

    def __init__(self, current: str | None, head: str) -> None:
        self.current, self.head = current, head
        state = "has no schema yet" if current is None else f"is at schema revision {current}"
        super().__init__(
            f"The database {state}; this application needs revision {head}. After taking a backup, run "
            "'python -m backend.migrate --env-file .env upgrade' explicitly."
        )


class StorageConflictError(DirectStorageError):
    """The change conflicts with data saved meanwhile (e.g. elsewhere); reload and try again."""


class NotSignedInError(DirectPersistenceError, PlanningError, ExecutionError):
    """The account session ended (signed out); sign in again."""

    def __init__(self) -> None:
        super().__init__("You are signed out. Sign in again to use the database.")


class AccountError(DirectPersistenceError):
    """An account operation was refused (the message is safe to show)."""


class InvalidCredentialsError(AccountError):
    """Unknown account or wrong password -- deliberately indistinguishable."""


class AccountExistsError(AccountError):
    """The email or username is already registered."""


class AccountValidationError(AccountError, ValueError):
    """The registration input breaks a rule."""
