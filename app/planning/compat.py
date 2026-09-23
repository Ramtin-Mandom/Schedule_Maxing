"""
app/planning/compat.py

Explicit migration adapters between the legacy scheduling models
(app/models.py, abstract 1-based day indexes, name-based dependencies) and
the canonical planning models (app/planning/models.py, calendar dates,
UUID identity and dependency references).

Nothing here changes app/data_processor.py, app/app.py's
parse_dependency_string, the live CSV schema, or Greedy Optimizer v1. This
module is additive: it gives a later milestone a single, tested place to
convert legacy data into canonical models, instead of re-deriving parsing
and identity rules at each call site.

Legacy day-index contract:
    day 1 == anchor_date
    day N == anchor_date + (N - 1) days
An anchor_date is always required explicitly -- this module never infers a
calendar date for a legacy day index from today's date (see
app.planning.time and the milestone's time-handling rules).

Identity contract:
    Legacy CSV rows and in-memory legacy Task/FixedBlock objects have no
    stable identity. Every conversion in this module mints a fresh UUID per
    legacy row/object, once, for that import. Re-running a conversion on
    the same (ID-less) legacy source produces a *new* set of IDs -- stable
    identity is only promised across edits/reimports of an already-
    canonical document (see PlanningDocument in app/planning/models.py),
    never across independent reimports of the same legacy CSV.

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
    "missing" as long as the caller includes it in `known_names`. This
    module's own day-scoped helpers (`convert_legacy_day_schedule`,
    `import_legacy_csv_rows`) only see one day's names; a future
    request-level importer that has visibility across days should build
    its own wider `known_names` map and call resolve_legacy_dependency_field
    directly for cross-day references.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta

from app import models as legacy_models
from app.planning.models import (
    DaySchedule as CanonicalDaySchedule,
)
from app.planning.models import (
    FixedBlock as CanonicalFixedBlock,
)
from app.planning.models import (
    LocalTimeWindow,
)
from app.planning.models import (
    Task as CanonicalTask,
)
from app.planning.models import (
    TaskRegistry,
)
from app.planning.time import LocalDayWindow, MINUTES_PER_DAY, UnsupportedSchedulingWindowError


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def legacy_day_to_date(day: int, anchor_date: date_) -> date_:
    """day 1 == anchor_date, day N == anchor_date + (N - 1) days."""
    if day < 1:
        raise ValueError(f"legacy day index must be >= 1, got {day}")
    return anchor_date + timedelta(days=day - 1)


def date_to_legacy_day(target: date_, anchor_date: date_) -> int:
    """Inverse of legacy_day_to_date. Raises if target precedes anchor_date."""
    delta = (target - anchor_date).days
    if delta < 0:
        raise ValueError(f"{target} is before anchor_date {anchor_date}; no valid legacy day index")
    return delta + 1


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


@dataclass
class LegacyImportResult:
    day_schedule: CanonicalDaySchedule
    task_ids_by_name: dict[str, list[uuid.UUID]]
    diagnostics: list[DependencyDiagnostic] = field(default_factory=list)


def _convert_fixed_blocks(
    fixed_blocks: list[legacy_models.FixedBlock],
    target_date: date_,
    tz_name: str,
) -> list[CanonicalFixedBlock]:
    converted: list[CanonicalFixedBlock] = []
    for block in fixed_blocks:
        window = _window_from_legacy_minutes(
            target_date, tz_name, block.time_window.start_time, block.time_window.end_time
        )
        start_utc, end_utc = window.to_utc_instants()
        converted.append(
            CanonicalFixedBlock(
                label=block.name,
                category=block.category or "fixed",
                planned_date=target_date,
                timezone=tz_name,
                planned_start=start_utc,
                planned_end=end_utc,
            )
        )
    return converted


def convert_legacy_day_schedule(
    legacy_day: legacy_models.DaySchedule,
    *,
    day_index: int,
    anchor_date: date_,
    tz_name: str,
) -> LegacyImportResult:
    """
    Convert an in-memory legacy DaySchedule (as produced by
    app.data_processor.load_schedule_from_csv or built up by app.app's
    ScheduleState) into a canonical DaySchedule.

    Legacy Task.dependencies here is whatever app/data_processor.py already
    parsed (its existing "-"-only split), so an already-mis-split
    hyphenated dependency name cannot be recovered at this stage. Prefer
    import_legacy_csv_rows, which applies the improved matching rules
    directly to the raw, unsplit CSV field, when the raw CSV is available.
    """
    target_date = legacy_day_to_date(day_index, anchor_date)

    fixed_blocks = _convert_fixed_blocks(legacy_day.fixed_blocks, target_date, tz_name)

    flexible_tasks = [task for task in legacy_day.tasks if not task.fixed]

    task_by_id: dict[uuid.UUID, CanonicalTask] = {}
    known_names: dict[str, list[uuid.UUID]] = {}
    entries: list[tuple[uuid.UUID, legacy_models.Task]] = []

    for legacy_task in flexible_tasks:
        task_id = uuid.uuid4()
        entries.append((task_id, legacy_task))
        known_names.setdefault(legacy_task.name, []).append(task_id)

        task_by_id[task_id] = CanonicalTask(
            id=task_id,
            name=legacy_task.name,
            category=legacy_task.category,
            tags=[legacy_task.tag] if legacy_task.tag else [],
            estimated_duration_minutes=legacy_task.duration,
            priority=legacy_task.priority,
            preferred_time_window=LocalTimeWindow(
                start_minute=legacy_task.preference_time.start_time,
                end_minute=min(legacy_task.preference_time.end_time, MINUTES_PER_DAY),
            ),
            preferred_dates=[target_date],
        )

    diagnostics: list[DependencyDiagnostic] = []
    for task_id, legacy_task in entries:
        dependency_ids, diags = _resolve_reference_list(legacy_task.name, legacy_task.dependencies, known_names)
        diagnostics.extend(diags)
        task_by_id[task_id] = task_by_id[task_id].model_copy(update={"dependency_ids": dependency_ids})

    registry = TaskRegistry(tasks=task_by_id)
    day_schedule = CanonicalDaySchedule(
        date=target_date,
        timezone=tz_name,
        fixed_blocks=fixed_blocks,
        task_ids=[task_id for task_id, _ in entries],
        tasks=registry,
    )

    return LegacyImportResult(day_schedule=day_schedule, task_ids_by_name=known_names, diagnostics=diagnostics)


# -----------------------------------------------------------------------------
# Raw legacy CSV rows -> canonical DaySchedule (new import adapter)
# -----------------------------------------------------------------------------


def _import_legacy_csv_day(
    day_rows: list[dict[str, str]],
    *,
    day_index: int,
    anchor_date: date_,
    tz_name: str,
) -> LegacyImportResult:
    target_date = legacy_day_to_date(day_index, anchor_date)

    fixed_blocks: list[CanonicalFixedBlock] = []
    task_by_id: dict[uuid.UUID, CanonicalTask] = {}
    known_names: dict[str, list[uuid.UUID]] = {}
    ordered_task_rows: list[tuple[uuid.UUID, dict[str, str]]] = []

    for row in day_rows:
        is_fixed = row["fixed"].strip().lower() == "true"

        if is_fixed:
            window = _window_from_legacy_minutes(
                target_date, tz_name, int(row["start_time"]), int(row["end_time"])
            )
            start_utc, end_utc = window.to_utc_instants()
            fixed_blocks.append(
                CanonicalFixedBlock(
                    label=row["name"],
                    category=(row.get("category") or "").strip() or "fixed",
                    planned_date=target_date,
                    timezone=tz_name,
                    planned_start=start_utc,
                    planned_end=end_utc,
                )
            )
            continue

        task_id = uuid.uuid4()
        tag = (row.get("tag") or "").strip()
        task_by_id[task_id] = CanonicalTask(
            id=task_id,
            name=row["name"],
            category=row["category"],
            tags=[tag] if tag else [],
            estimated_duration_minutes=int(row["duration"]),
            priority=int(row["priority"]),
            preferred_time_window=LocalTimeWindow(
                start_minute=int(row["start_time"]),
                end_minute=min(int(row["end_time"]), MINUTES_PER_DAY),
            ),
            preferred_dates=[target_date],
        )
        known_names.setdefault(row["name"], []).append(task_id)
        ordered_task_rows.append((task_id, row))

    diagnostics: list[DependencyDiagnostic] = []
    for task_id, row in ordered_task_rows:
        raw_field = row.get("dependencies", "") or ""
        dependency_ids, diags = resolve_legacy_dependency_field(row["name"], raw_field, known_names)
        diagnostics.extend(diags)
        task_by_id[task_id] = task_by_id[task_id].model_copy(update={"dependency_ids": dependency_ids})

    registry = TaskRegistry(tasks=task_by_id)
    day_schedule = CanonicalDaySchedule(
        date=target_date,
        timezone=tz_name,
        fixed_blocks=fixed_blocks,
        task_ids=[task_id for task_id, _ in ordered_task_rows],
        tasks=registry,
    )

    return LegacyImportResult(day_schedule=day_schedule, task_ids_by_name=known_names, diagnostics=diagnostics)


def import_legacy_csv_rows(
    rows: list[dict[str, str]],
    *,
    anchor_date: date_,
    tz_name: str,
) -> dict[int, LegacyImportResult]:
    """
    Convert legacy schedule CSV rows (the same row shape as
    app.data_processor.read_csv_rows) into one canonical DaySchedule per
    legacy day index found in the rows.

    This is a new, additive import path -- it does not read from or write
    to app/data_processor.py, and the live CSV schema is unchanged. Each
    day is converted independently in a two-pass process: pass 1 mints a
    fresh UUID and canonical Task for every flexible-task row (and a
    canonical FixedBlock for every fixed row); pass 2 resolves each
    flexible task's raw `dependencies` field into dependency_ids via
    resolve_legacy_dependency_field.
    """
    rows_by_day: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_day.setdefault(int(row["date"]), []).append(row)

    return {
        day_index: _import_legacy_csv_day(day_rows, day_index=day_index, anchor_date=anchor_date, tz_name=tz_name)
        for day_index, day_rows in rows_by_day.items()
    }
