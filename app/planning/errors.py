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


class StaleInputsError(PlanningError):
    """
    A generation (or a preview it was based on) read scheduling inputs that
    have changed since: the inputs fingerprint recomputed now differs from
    the one it expected. Nothing was saved; the previous schedule stands.
    """

    def __init__(self, expected: str, current: str, message: str | None = None) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            message
            or "The tasks, fixed blocks, preferences or dependencies of this range changed since they were read. "
            "Nothing was saved; preview the range again."
        )


class RegenerationRequiredError(PlanningError):
    """
    Placements that an incremental generation (or protected history) would
    keep are no longer compatible with the current inputs; `problems` says
    which and why. Nothing was saved -- an explicit (full) regeneration is
    needed, which never happens implicitly.
    """

    def __init__(self, problems: list, message: str | None = None) -> None:
        self.problems = list(problems)
        super().__init__(
            message
            or "Some saved placements no longer fit the current inputs, so they cannot be kept: "
            + "; ".join(f"{problem.placement_id} ({problem.reason})" for problem in self.problems)
            + ". Nothing was saved; regenerate explicitly to replace them."
        )


class VersionConflictError(PlanningError):
    """
    Raised when a write's precondition does not hold: the caller's
    expected_version is not the stored record's current version (someone
    else changed it since the caller read it), or the record has since been
    deleted (current_version is then the tombstone's version and `deleted`
    is True). The stored record is left exactly as it was.
    """

    def __init__(
        self,
        kind: str,
        entity_id: object,
        *,
        expected_version: int | None,
        current_version: int | None,
        deleted: bool = False,
        message: str | None = None,
    ) -> None:
        self.kind = kind
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.current_version = current_version
        self.deleted = deleted
        if message is None:
            state = "has been deleted" if deleted else f"is at version {current_version}"
            message = (
                f"The {kind} {entity_id} was changed by someone else: expected version {expected_version}, "
                f"but it {state}. Reload it and try again."
            )
        super().__init__(message)


class HistoryProtectedError(PlanningError):
    """
    An explicit reschedule was refused because the placement's execution has
    already started or finished (`status` is its execution status): moving it
    would rewrite the history of an attempt. Nothing was changed.
    """

    def __init__(self, placement_id: object, status: str) -> None:
        self.placement_id = placement_id
        self.status = status
        super().__init__(
            f"The placement {placement_id} cannot be rescheduled: its execution is {status}, and started or "
            "finished work is history. Nothing was changed."
        )


class RescheduleRejectedError(PlanningError):
    """
    An explicit reschedule's destination breaks a hard scheduling rule;
    `problems` (app.planning.workflow.PlacementProblem) says which and why.
    Nothing was changed.
    """

    def __init__(self, problems: list, message: str | None = None) -> None:
        self.problems = list(problems)
        super().__init__(
            message
            or "The placement cannot be moved there: "
            + "; ".join(f"{problem.explanation} ({problem.reason})" for problem in self.problems)
            + " Nothing was changed."
        )
