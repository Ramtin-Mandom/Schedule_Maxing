"""
app/planning/compat.py

Helpers for the legacy schedule CSV format (app/planning/csv_import.py and
the schedule pages): abstract 1-based day indexes and minutes-from-midnight
converted to calendar dates and aware instants, and name-based dependency
references resolved to task ids.

Legacy day-index contract:
    day 1 == anchor_date
    day N == anchor_date + (N - 1) days
An anchor_date is always required explicitly -- this module never infers a
calendar date for a legacy day index from today's date (see
app.planning.time and the milestone's time-handling rules).

Identity contract:
    Legacy CSV rows have no stable identity: an import mints a fresh UUID
    per row, so re-importing the same file produces a new set of IDs.

Dependency resolution contract (see resolve_legacy_dependency_field):
    1. An explicit semicolon-separated reference list is always honored
       verbatim (each piece matched by exact name).
    2. Otherwise, the whole raw field is tried as a single exact-name
       reference first, so hyphenated task names (e.g. "Pre-Calc Review")
       are preserved rather than split.
    3. Only if that whole-field match fails is the field split on "-", and
       only used if every resulting piece resolves to exactly one known
       task name.
    4. A reference matching more than one task with the same name is
       reported as an "ambiguous" diagnostic and dropped -- never guessed.
    5. A reference matching no known task name is reported as a "missing"
       diagnostic and dropped, preserving the existing legacy semantics
       that missing dependency names are ignored rather than treated as a
       hard error.

    `known_names` is caller-supplied and may span more than the tasks on
    the current day: a reference to a task that is genuinely known (e.g.
    from a wider multi-day planning request) but happens to live outside
    the day currently being converted is therefore not misreported as
    "missing" as long as the caller includes it in `known_names`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timedelta

from app.planning.time import LocalDayWindow, MINUTES_PER_DAY, UnsupportedSchedulingWindowError


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def legacy_day_to_date(day: int, anchor_date: date_) -> date_:
    """day 1 == anchor_date, day N == anchor_date + (N - 1) days."""
    if day < 1:
        raise ValueError(f"legacy day index must be >= 1, got {day}")
    return anchor_date + timedelta(days=day - 1)


def _window_from_legacy_minutes(
    day: date_,
    tz_name: str,
    start_minute: int,
    end_minute: int,
) -> LocalDayWindow:
    """
    Legacy minutes-from-midnight (0-1440, where 1440 means "the following
    midnight") -> an explicit LocalDayWindow. A legacy end_time of exactly
    1440 and a same-day midnight both resolve to the identical instant
    (the following date at 00:00); anything past 1440 is not representable
    within one legacy day and is rejected.
    """
    if end_minute > MINUTES_PER_DAY:
        raise UnsupportedSchedulingWindowError(
            f"legacy end_time {end_minute} is beyond 24:00 (1440) and is not supported yet"
        )
    if end_minute == MINUTES_PER_DAY:
        return LocalDayWindow(day=day, tz_name=tz_name, start_minute=start_minute, end_minute=0, end_day_offset=1)
    return LocalDayWindow(day=day, tz_name=tz_name, start_minute=start_minute, end_minute=end_minute, end_day_offset=0)


def legacy_minutes_to_utc(
    day: date_,
    tz_name: str,
    start_minute: int,
    end_minute: int,
) -> tuple[datetime, datetime]:
    """
    Public form of the legacy minutes-from-midnight contract above: the aware
    UTC (start, end) instants of [start_minute, end_minute) on local `day` in
    `tz_name` (end_minute == 1440 means the following local midnight).
    """
    return _window_from_legacy_minutes(day, tz_name, start_minute, end_minute).to_utc_instants()


# -----------------------------------------------------------------------------
# Dependency-name resolution
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyDiagnostic:
    task_name: str
    raw_reference: str
    kind: str  # "missing" | "ambiguous"
    message: str


def _resolve_single_reference(
    task_name: str,
    reference: str,
    known_names: dict[str, list[uuid.UUID]],
) -> tuple[list[uuid.UUID] | None, list[DependencyDiagnostic]]:
    matches = known_names.get(reference, [])

    if len(matches) == 1:
        return [matches[0]], []

    if len(matches) > 1:
        return None, [
            DependencyDiagnostic(
                task_name=task_name,
                raw_reference=reference,
                kind="ambiguous",
                message=(
                    f"dependency reference {reference!r} matches {len(matches)} tasks "
                    "sharing that name; specify the intended task explicitly (ignored)"
                ),
            )
        ]

    # Genuinely absent: ignored with a diagnostic, matching existing legacy
    # missing-dependency semantics (see app/pert.py, README's Dependency
    # Behavior section).
    return None, [
        DependencyDiagnostic(
            task_name=task_name,
            raw_reference=reference,
            kind="missing",
            message=f"dependency reference {reference!r} does not match any known task name (ignored)",
        )
    ]


def _resolve_reference_list(
    task_name: str,
    references: list[str],
    known_names: dict[str, list[uuid.UUID]],
) -> tuple[list[uuid.UUID], list[DependencyDiagnostic]]:
    resolved: list[uuid.UUID] = []
    diagnostics: list[DependencyDiagnostic] = []

    for reference in references:
        ids, diags = _resolve_single_reference(task_name, reference, known_names)
        diagnostics.extend(diags)
        if ids is not None:
            resolved.extend(ids)

    return resolved, diagnostics


def resolve_legacy_dependency_field(
    task_name: str,
    raw_field: str,
    known_names: dict[str, list[uuid.UUID]],
) -> tuple[list[uuid.UUID], list[DependencyDiagnostic]]:
    """
    Resolve one flexible task's raw legacy `dependencies` CSV field into
    canonical dependency_ids, per the module docstring's resolution
    contract. `known_names` maps a task name to every known task id sharing
    that name (order not significant).
    """
    raw_field = (raw_field or "").strip()
    if not raw_field:
        return [], []

    if ";" in raw_field:
        references = [part.strip() for part in raw_field.split(";") if part.strip()]
        return _resolve_reference_list(task_name, references, known_names)

    # Try the whole field as one exact reference first, so a hyphenated
    # task name is preserved instead of being split.
    whole_ids, whole_diagnostics = _resolve_single_reference(task_name, raw_field, known_names)
    if whole_ids is not None:
        return whole_ids, []

    # Fall back to the old hyphen-delimited sample-data convention, but
    # only when every resulting piece resolves unambiguously.
    if "-" in raw_field:
        pieces = [part.strip() for part in raw_field.split("-") if part.strip()]
        piece_ids: list[uuid.UUID] = []
        for piece in pieces:
            ids, _piece_diagnostics = _resolve_single_reference(task_name, piece, known_names)
            if ids is None:
                # The hyphen split did not resolve unambiguously either;
                # report against the original whole-field reference so the
                # diagnostic reflects what the user actually wrote.
                return [], whole_diagnostics
            piece_ids.extend(ids)
        return piece_ids, []

    return [], whole_diagnostics


# -----------------------------------------------------------------------------
# Legacy in-memory DaySchedule -> canonical DaySchedule
# -----------------------------------------------------------------------------
