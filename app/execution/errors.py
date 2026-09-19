"""
errors.py

Domain exceptions for the execution-tracking subsystem.

These are raised by app/execution/service.py (and, for not-found cases,
app/execution/repository.py) instead of letting sqlite errors or KeyErrors
leak out, so callers get a clear, typed reason for the failure.
"""

from __future__ import annotations

from app.execution.models import ExecutionStatus


class ExecutionError(Exception):
    """Base class for all execution-tracking domain errors."""


class ExecutionNotFoundError(ExecutionError):
    """Raised when an execution id does not exist in the database."""

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        super().__init__(f"No execution found with id={execution_id!r}.")


class InvalidTransitionError(ExecutionError):
    """Raised when a requested status transition is not allowed."""

    def __init__(
        self,
        execution_id: str,
        current_status: ExecutionStatus,
        attempted_status: ExecutionStatus,
    ) -> None:
        self.execution_id = execution_id
        self.current_status = current_status
        self.attempted_status = attempted_status
        super().__init__(
            f"Cannot transition execution {execution_id!r} from "
            f"'{current_status.value}' to '{attempted_status.value}'."
        )


class InvalidFeedbackError(ExecutionError):
    """Raised when feedback values (ratings, interruption count) are out of range."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
