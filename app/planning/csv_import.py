"""
app/planning/csv_import.py

The transactional CSV import boundary for persisted planning data
(Milestone 2). A legacy schedule CSV (the established `date,name,category,
tag,fixed,start_time,end_time,duration,priority,dependencies` format, see
samples/inputs/) is parsed and validated *completely* into canonical
models first; only then is the whole result written through
PlanningService.apply_import in one transaction. Invalid input writes
nothing, and a failure while writing rolls everything back -- including a
replace's deletions.

Conversion reuses the app.planning.compat contract unchanged:
    - day N == anchor_date + (N - 1) days; the anchor date and IANA
      timezone are always explicit (never inferred from today);
    - start_time/end_time are local minutes from midnight (1440 = the next
      midnight) converted with compat.legacy_minutes_to_utc;
    - a flexible row becomes a Task with preferred_dates=[its date] and its
      window as preferred_time_window; a fixed row becomes a FixedBlock
      whose category is the row's category ("fixed" when empty);
    - the `dependencies` field is interpreted by
      compat.resolve_legacy_dependency_field (semicolon lists, whole-field
      match that preserves hyphenated names, then an unambiguous hyphen
      split).

Stricter than the standalone compat adapters (which keep their legacy
"drop and report" behavior for their own callers):
    - relationships are resolved across the complete import: a reference is
      matched against the same day's task names first and, only if that
      name does not occur on that day, against every day of the file
      (cross-day references); a name that is still ambiguous (duplicated) or
      missing is an import *error*, never guessed and never silently
      dropped. Dependencies on already-stored tasks are not resolved by
      name (the file cannot name them unambiguously).
    - a self-reference or a dependency cycle among the imported tasks, a
      fixed block overlapping another in the file (or, for append, a stored
      block on the same date), and any malformed value are errors, reported
      with the CSV line number.
Times are not forced onto the desktop form's 30-minute grid: the canonical
engine is minute-precise.

Identity: legacy rows carry no ids. Every import mints a fresh UUID for
each row, once; appending the same file twice therefore creates a second,
distinct set of tasks. The identity-preserving format is the canonical
stored-planning CSV (app/planning/csv_export.py writes it,
app/planning/csv_canonical.py imports it); callers tell the two apart by
header (csv_canonical.is_canonical_csv).

Modes (ImportMode):
    - APPEND adds every imported task and fixed block; nothing existing is
      changed.
    - REPLACE first clears the file's own date span -- every date from its
      first to its last day, inclusive -- exactly like the desktop's
      "schedule, fixed blocks, and tasks" reset (PlanningService.clear_range:
      placements, fixed blocks, and tasks planned in that span; undated
      tasks and anything outside the span are kept), then adds the import.
      Execution history is never deleted; placements it references are
      reported. If a task outside the span depends on one inside it, the
      whole import is refused.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date as date_
from enum import Enum
from pathlib import Path

from app.pert import has_cycle_by_id
from app.planning.compat import legacy_day_to_date, legacy_minutes_to_utc, resolve_legacy_dependency_field
from app.planning.errors import PlanningError
from app.planning.models import FixedBlock, LocalTimeWindow, Task
from app.planning.time import MINUTES_PER_DAY, validate_timezone

REQUIRED_COLUMNS = ("date", "name", "category", "fixed", "start_time", "end_time")
FLEXIBLE_COLUMNS = ("duration", "priority")
MAX_REPORTED_ERRORS = 20


class ImportMode(str, Enum):
    APPEND = "append"
    REPLACE = "replace"


@dataclass(frozen=True)
class ImportIssue:
    line: int | None
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}" if self.line is not None else self.message


class CsvImportError(PlanningError):
    """The CSV is invalid; nothing was written. `issues` lists every problem found."""

    def __init__(self, issues: list[ImportIssue]) -> None:
        self.issues = issues
        shown = "\n".join(f"  - {issue}" for issue in issues[:MAX_REPORTED_ERRORS])
        more = f"\n  ... and {len(issues) - MAX_REPORTED_ERRORS} more" if len(issues) > MAX_REPORTED_ERRORS else ""
        super().__init__(f"The CSV was not imported ({len(issues)} problem(s); nothing was saved):\n{shown}{more}")


@dataclass(frozen=True)
class ParsedImport:
    """A fully validated import, ready to be written in one transaction."""

    anchor_date: date_
    timezone: str
    start_date: date_
    end_date: date_
    tasks: list[Task]
    fixed_blocks: list[FixedBlock]
    source: str = ""
    lines_by_task_id: dict[uuid.UUID, int] = field(default_factory=dict)


def read_csv_text(path: str | Path) -> str:
    """Read a CSV file as text (a UTF-8 byte-order mark, as written by spreadsheet apps, is accepted)."""
    return Path(path).read_text(encoding="utf-8-sig")


def parse_legacy_csv(text: str, *, anchor_date: date_, timezone: str, source: str = "") -> ParsedImport:
    """Parse and fully validate legacy CSV text. Raises CsvImportError listing every problem."""
    try:
        validate_timezone(timezone)
    except ValueError as error:
        raise CsvImportError([ImportIssue(None, str(error))]) from error

    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in (reader.fieldnames or [])]
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing_columns:
        raise CsvImportError([ImportIssue(1, f"missing required column(s): {', '.join(missing_columns)}")])

    issues: list[ImportIssue] = []
    tasks: list[tuple[int, date_, Task, str]] = []  # (line, date, task, raw dependencies)
    blocks: list[tuple[int, FixedBlock]] = []

    for index, raw_row in enumerate(reader):
        line = index + 2  # the header is line 1
        row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items() if key is not None}
        if not any(row.values()):
            continue  # a blank line
        try:
            parsed = _parse_row(row, anchor_date, timezone)
        except _RowError as error:
            issues.append(ImportIssue(line, str(error)))
            continue
        if isinstance(parsed, FixedBlock):
            blocks.append((line, parsed))
        else:
            task, day = parsed
            tasks.append((line, day, task, row.get("dependencies", "")))

    if not tasks and not blocks and not issues:
        issues.append(ImportIssue(None, "the CSV contains no rows"))

    issues.extend(_overlapping_blocks(blocks))
    resolved_tasks = _resolve_dependencies(tasks, issues)

    if not issues and has_cycle_by_id({task.id: task for task in resolved_tasks}):
        issues.append(ImportIssue(None, "the dependencies among the imported tasks form a cycle"))

    if issues:
        raise CsvImportError(sorted(issues, key=lambda issue: (issue.line is None, issue.line or 0)))

    dates = [day for _, day, _, _ in tasks] + [block.planned_date for _, block in blocks]
    return ParsedImport(
        anchor_date=anchor_date,
        timezone=timezone,
        start_date=min(dates),
        end_date=max(dates),
        tasks=resolved_tasks,
        fixed_blocks=[block for _, block in blocks],
        source=source,
        lines_by_task_id={task.id: line for line, _, task, _ in tasks},
    )


def parse_legacy_csv_file(path: str | Path, *, anchor_date: date_, timezone: str) -> ParsedImport:
    try:
        text = read_csv_text(path)
    except (OSError, UnicodeDecodeError) as error:
        raise CsvImportError([ImportIssue(None, f"could not read {path}: {error}")]) from error
    return parse_legacy_csv(text, anchor_date=anchor_date, timezone=timezone, source=str(path))


# -----------------------------------------------------------------------------
# Rows
# -----------------------------------------------------------------------------


class _RowError(ValueError):
    pass


def _int(row: dict[str, str], column: str) -> int:
    value = row.get(column, "")
    if value == "":
        raise _RowError(f"{column} is required")
    try:
        return int(value)
    except ValueError as error:
        raise _RowError(f"{column} must be a whole number, got {value!r}") from error


def _parse_row(row: dict[str, str], anchor_date: date_, timezone: str) -> FixedBlock | tuple[Task, date_]:
    name = row.get("name", "")
    if not name:
        raise _RowError("name is required")

    fixed_text = row.get("fixed", "").lower()
    if fixed_text not in ("true", "false"):
        raise _RowError(f"fixed must be true or false, got {row.get('fixed', '')!r}")

    day_index = _int(row, "date")
    if day_index < 1:
        raise _RowError(f"date (day index) must be 1 or more, got {day_index}")
    day = legacy_day_to_date(day_index, anchor_date)

    start, end = _int(row, "start_time"), _int(row, "end_time")
    if not 0 <= start < MINUTES_PER_DAY or not 0 < end <= MINUTES_PER_DAY:
        raise _RowError(f"start_time/end_time must be within 0-1440 minutes, got {start}-{end}")
    if end <= start:
        raise _RowError(f"end_time ({end}) must be after start_time ({start})")

    if fixed_text == "true":
        try:
            start_utc, end_utc = legacy_minutes_to_utc(day, timezone, start, end)
            return FixedBlock(
                label=name, category=row.get("category") or "fixed", planned_date=day, timezone=timezone,
                planned_start=start_utc, planned_end=end_utc,
            )
        except ValueError as error:
            raise _RowError(f"fixed block {name!r}: {_message(error)}") from error

    for column in FLEXIBLE_COLUMNS:
        if column not in row:
            raise _RowError(f"missing column {column!r} for a flexible task")
    category = row.get("category", "")
    if not category:
        raise _RowError("category is required for a flexible task")
    duration, priority = _int(row, "duration"), _int(row, "priority")
    if duration <= 0:
        raise _RowError(f"duration must be greater than 0, got {duration}")
    if not 1 <= priority <= 10:
        raise _RowError(f"priority must be between 1 and 10, got {priority}")
    tag = row.get("tag", "")
    try:
        task = Task(
            name=name,
            category=category,
            tags=[tag] if tag else [],
            estimated_duration_minutes=duration,
            priority=priority,
            preferred_time_window=LocalTimeWindow(start_minute=start, end_minute=end),
            preferred_dates=[day],
        )
    except ValueError as error:
        raise _RowError(f"task {name!r}: {_message(error)}") from error
    return task, day


def _message(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        details = errors()
        if details:
            return str(details[0].get("msg", error))
    return str(error)


def _overlapping_blocks(blocks: list[tuple[int, FixedBlock]]) -> list[ImportIssue]:
    issues = []
    by_date: dict[date_, list[tuple[int, FixedBlock]]] = {}
    for line, block in blocks:
        by_date.setdefault(block.planned_date, []).append((line, block))
    for entries in by_date.values():
        ordered = sorted(entries, key=lambda entry: entry[1].planned_start)
        for (_, earlier), (line, later) in zip(ordered, ordered[1:]):
            if later.planned_start < earlier.planned_end:
                issues.append(ImportIssue(
                    line, f"fixed block {later.label!r} overlaps fixed block {earlier.label!r} on {later.planned_date}"
                ))
    return issues


def _resolve_dependencies(tasks: list[tuple[int, date_, Task, str]], issues: list[ImportIssue]) -> list[Task]:
    """Same-day names first, then the whole file; ambiguous/missing/self references are issues."""
    all_days: dict[str, list[uuid.UUID]] = {}
    by_day: dict[date_, dict[str, list[uuid.UUID]]] = {}
    for _, day, task, _ in tasks:
        all_days.setdefault(task.name, []).append(task.id)
        by_day.setdefault(day, {}).setdefault(task.name, []).append(task.id)

    resolved: list[Task] = []
    for line, day, task, raw_dependencies in tasks:
        same_day = by_day[day]
        scoped = {name: same_day.get(name, ids) for name, ids in all_days.items()}
        dependency_ids, diagnostics = resolve_legacy_dependency_field(task.name, raw_dependencies, scoped)
        for diagnostic in diagnostics:
            reason = (
                "matches more than one imported task with that name; rename the tasks so the reference is unique"
                if diagnostic.kind == "ambiguous"
                else "does not match any task in this file"
            )
            issues.append(ImportIssue(line, f"dependency {diagnostic.raw_reference!r} of {task.name!r} {reason}"))
        if task.id in dependency_ids:
            issues.append(ImportIssue(line, f"task {task.name!r} depends on itself"))
            dependency_ids = [dependency for dependency in dependency_ids if dependency != task.id]
        resolved.append(task.model_copy(update={"dependency_ids": list(dict.fromkeys(dependency_ids))}))
    return resolved
