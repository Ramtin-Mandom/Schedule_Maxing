"""Typed errors of the budget domain (app/budget)."""

from __future__ import annotations


class BudgetError(Exception):
    """Base class of every budget error."""


class BudgetValidationError(BudgetError, ValueError):
    """An amount, date, category, recurrence or link is not acceptable."""


class BudgetNotFoundError(BudgetError, LookupError):
    """No live budget record with this id exists in the caller's workspace."""


class BudgetConflictError(BudgetError):
    """The record changed since it was read (a stale version), or the change conflicts with stored data."""
