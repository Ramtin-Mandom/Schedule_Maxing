"""
app/planning/errors.py

Domain/persistence exceptions for persisted canonical planning data
(app/planning/repository.py and app/planning/application.py), mirroring
app/execution/errors.py: callers get a typed, readable reason instead of a
raw sqlite3 error or KeyError.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable


class PlanningError(Exception):
    """Base class for planning persistence/domain errors."""


class EntityNotFoundError(PlanningError):
    """Raised when a task/project/fixed block/placement id does not exist."""

    def __init__(self, kind: str, entity_id: uuid.UUID) -> None:
        self.kind = kind
        self.entity_id = entity_id
        super().__init__(f"No {kind} found with id={entity_id}.")


class DuplicateEntityError(PlanningError):
    """Raised by an explicit create when the id is already persisted."""

    def __init__(self, kind: str, entity_id: uuid.UUID) -> None:
        self.kind = kind
        self.entity_id = entity_id
        super().__init__(f"A {kind} with id={entity_id} already exists.")


class InvalidReferenceError(PlanningError):
    """Raised when an entity references ids that are not persisted (e.g. a
    dependency or project that does not exist)."""

    def __init__(self, message: str, missing_ids: Iterable[uuid.UUID] = ()) -> None:
        self.missing_ids = sorted(missing_ids, key=str)
        super().__init__(message)


class EntityInUseError(PlanningError):
    """Raised when deleting an entity other planning data still depends on
    (a task other tasks depend on, or a project that still has tasks)."""

    def __init__(self, message: str, dependent_ids: Iterable[uuid.UUID] = ()) -> None:
        self.dependent_ids = sorted(dependent_ids, key=str)
        super().__init__(message)


class ScopeError(PlanningError):
    """Raised when a scoped write (e.g. replacing one date range's placements
    or one date's fixed blocks) would touch data outside its scope."""


class InvalidEntityError(PlanningError):
    """Raised when an entity cannot be stored as given (e.g. non-JSON
    optimization_metadata)."""
