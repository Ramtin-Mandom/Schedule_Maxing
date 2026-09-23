"""
app/planning/csv_canonical.py

Import of the canonical stored-planning CSV (format version 2, written by
app/planning/csv_export.py -- see that module for the column contract):
an identity-preserving round trip. Where the legacy schedule CSV
(app/planning/csv_import.py) has no ids and always creates new records,
this format carries every record's id, owner, relationships (project,
dependencies, placement task), local edit revision, audit timestamps,
tombstone, fixed-block category, recurrence rule, and placement metadata,
so exporting and importing reproduces the same records.

Two stages, both all-or-nothing:

1. parse_canonical_csv / parse_canonical_csv_file (this module) validate the
   whole file *format* -- header, format version, record types, every
   value's type (UUIDs, dates, aware ISO instants, JSON arrays/objects),
   model rules, duplicate ids -- and check the derived columns (a task's
   `date`; a block's/placement's local times and duration) against the
   authoritative ones, so an edit to an informational column is reported
   instead of silently ignored. Every problem is reported at once, with
   its CSV line, in a CsvImportError; nothing is written.

2. PlanningService.apply_record_batch validates the batch against what is
   stored (ownership, relationships, collisions, cycles, overlaps) and
   applies it in one transaction. Its documented collision rules decide
   what an existing id means: identical content is a no-op; a divergent
   record needs allow_updates *and* its row's `version` equal to the stored
   version (a version value in the file can never bypass concurrency);
   a deleted record is never revived.

A row with an empty `id` gets a fresh UUID (like a legacy import); other
rows cannot reference it. A non-empty id must be a valid UUID and is kept
exactly. Format version 1 files (no ownership/recurrence/project columns)
are refused rather than imported with those fields silently cleared.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date as date_
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from app.planning.application import RecordBatch, task_planned_date
from app.planning.csv_export import COLUMNS, FORMAT_VERSION
from app.planning.csv_import import CsvImportError, ImportIssue, read_csv_text
from app.planning.models import FixedBlock, LocalTimeWindow, Project, RecurrenceSpec, ScheduledTask, Task
from app.planning.time import local_minutes, minutes_to_hhmm

RECORD_TYPES = ("project", "task", "fixed_block", "placement")


def is_canonical_csv(text: str) -> bool:
    """True if the header identifies a stored-planning (identity-bearing) CSV rather than a legacy schedule CSV."""
    header = next(csv.reader(io.StringIO(text)), [])
    names = {name.strip() for name in header}
    return {"record_type", "id"} <= names


def parse_canonical_csv_file(path: str | Path) -> RecordBatch:
    try:
        text = read_csv_text(path)
    except (OSError, UnicodeDecodeError) as error:
        raise CsvImportError([ImportIssue(None, f"could not read {path}: {error}")]) from error
    return parse_canonical_csv(text)


def parse_canonical_csv(text: str) -> RecordBatch:
    """Parse and fully validate a canonical planning CSV. Raises CsvImportError listing every problem."""
    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in (reader.fieldnames or [])]
    if "format_version" not in header:
        raise CsvImportError([ImportIssue(
            1, "this is a format version 1 planning export, which has no ownership, project, recurrence, or "
            "deletion columns; importing it could silently clear those fields. Export again with this version."
        )])
    missing = [column for column in COLUMNS if column not in header]
    if missing:
        raise CsvImportError([ImportIssue(1, f"missing column(s): {', '.join(missing)}")])

    issues: list[ImportIssue] = []
    batch: dict[str, list[tuple[int, object]]] = {kind: [] for kind in RECORD_TYPES}
    for index, raw_row in enumerate(reader):
        line = index + 2
        row = {(key or "").strip(): (value if value is not None else "") for key, value in raw_row.items() if key is not None}
        if not any(value.strip() for value in row.values()):
            continue
        try:
            record_type = row["record_type"].strip()
            if record_type not in RECORD_TYPES:
                raise _RowError(f"record_type must be one of {', '.join(RECORD_TYPES)}, got {record_type!r}")
            if row["format_version"].strip() != str(FORMAT_VERSION):
                raise _RowError(f"format_version must be {FORMAT_VERSION}, got {row['format_version']!r}")
            batch[record_type].append((line, _PARSERS[record_type](row)))
        except ValidationError as error:
            issues.append(ImportIssue(line, _validation_message(error)))
        except ValueError as error:  # _RowError, or a malformed UUID/date inside a JSON list
            issues.append(ImportIssue(line, str(error)))

    if not any(batch.values()) and not issues:
        issues.append(ImportIssue(None, "the CSV contains no records"))

    for kind, entries in batch.items():
        seen: dict[uuid.UUID, int] = {}
        for line, record in entries:
            if record.id in seen:
                issues.append(ImportIssue(line, f"{kind} id {record.id} already appears on line {seen[record.id]}"))
            seen.setdefault(record.id, line)

    tasks = {record.id: record for _, record in batch["task"]}
    for line, placement in batch["placement"]:
        task = tasks.get(placement.task_id)
        if task is not None and placement.user_id != task.user_id:
            issues.append(ImportIssue(line, "a placement must have the same user_id as its task"))

    if issues:
        raise CsvImportError(sorted(issues, key=lambda issue: (issue.line is None, issue.line or 0)))

    return RecordBatch(
        projects=[record for _, record in batch["project"]],
        tasks=[record for _, record in batch["task"]],
        fixed_blocks=[record for _, record in batch["fixed_block"]],
        placements=[record for _, record in batch["placement"]],
    )


# -----------------------------------------------------------------------------
# Rows
# -----------------------------------------------------------------------------


class _RowError(ValueError):
    pass


def _validation_message(error: ValidationError) -> str:
    details = error.errors()
    if not details:
        return str(error)
    first = details[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', error)}" if location else str(first.get("msg", error))


def _text(row: dict[str, str], column: str) -> str:
    return row.get(column, "").strip()


def _uuid(row: dict[str, str], column: str, *, required: bool) -> uuid.UUID | None:
    value = _text(row, column)
    if not value:
        if required:
            raise _RowError(f"{column} is required")
        return None
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise _RowError(f"{column} must be a UUID, got {value!r}") from error


def _record_id(row: dict[str, str]) -> uuid.UUID:
    return _uuid(row, "id", required=False) or uuid.uuid4()


def _int(row: dict[str, str], column: str) -> int:
    value = _text(row, column)
    try:
        return int(value)
    except ValueError as error:
        raise _RowError(f"{column} must be a whole number, got {value!r}") from error


def _date(row: dict[str, str], column: str, *, required: bool) -> date_ | None:
    value = _text(row, column)
    if not value:
        if required:
            raise _RowError(f"{column} is required")
        return None
    try:
        return date_.fromisoformat(value)
    except ValueError as error:
        raise _RowError(f"{column} must be a date (YYYY-MM-DD), got {value!r}") from error


def _instant(row: dict[str, str], column: str, *, required: bool) -> datetime | None:
    value = _text(row, column)
    if not value:
        if required:
            raise _RowError(f"{column} is required")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _RowError(f"{column} must be an ISO 8601 date-time, got {value!r}") from error
    if parsed.tzinfo is None:
        raise _RowError(f"{column} must include a UTC offset, got {value!r}")
    return parsed


def _json(row: dict[str, str], column: str, expected: type, *, empty):
    value = _text(row, column)
    if not value:
        return empty
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise _RowError(f"{column} must be JSON, got {value!r}") from error
    if not isinstance(parsed, expected):
        raise _RowError(f"{column} must be a JSON {expected.__name__}, got {value!r}")
    return parsed


def _hhmm(text: str, column: str) -> int:
    try:
        hours, minutes = text.split(":")
        value = int(hours) * 60 + int(minutes)
    except ValueError as error:
        raise _RowError(f"{column} must be HH:MM-HH:MM, got {text!r}") from error
    if not 0 <= value <= 1440 or not 0 <= int(minutes) < 60:
        raise _RowError(f"{column} must be HH:MM-HH:MM within 00:00-24:00, got {text!r}")
    return value


def _audit(row: dict[str, str]) -> dict:
    return {
        "user_id": _uuid(row, "user_id", required=False),
        "version": _int(row, "version"),
        "created_at": _instant(row, "created_at", required=True),
        "updated_at": _instant(row, "updated_at", required=True),
        "deleted_at": _instant(row, "deleted_at", required=False),
    }


def _check_interval(row: dict[str, str], start: datetime, end: datetime, day: date_, tz_name: str) -> None:
    """The derived local-time/duration columns must agree with the authoritative UTC instants."""
    expected = {
        "start_local": minutes_to_hhmm(local_minutes(start, day, tz_name)),
        "end_local": minutes_to_hhmm(local_minutes(end, day, tz_name)),
        "duration_minutes": f"{(end - start).total_seconds() / 60:g}",
    }
    for column, value in expected.items():
        given = _text(row, column)
        if given and given != value:
            raise _RowError(
                f"{column} is {given!r} but start_utc/end_utc say {value!r}; start_utc/end_utc are authoritative "
                "(edit those, or clear the derived column)"
            )


def _parse_project(row: dict[str, str]) -> Project:
    return Project(id=_record_id(row), name=_text(row, "name"), description=row.get("description") or None, **_audit(row))


def _parse_task(row: dict[str, str]) -> Task:
    window_text = _text(row, "preferred_window")
    window = None
    if window_text:
        if "-" not in window_text:
            raise _RowError(f"preferred_window must be HH:MM-HH:MM, got {window_text!r}")
        start_text, end_text = window_text.split("-", 1)
        window = LocalTimeWindow(
            start_minute=_hhmm(start_text, "preferred_window"), end_minute=_hhmm(end_text, "preferred_window")
        )
    required_text = _text(row, "required").lower()
    if required_text not in ("true", "false"):
        raise _RowError(f"required must be true or false, got {row.get('required')!r}")

    recurrence = _json(row, "recurrence", dict, empty=None)
    task = Task(
        id=_record_id(row),
        project_id=_uuid(row, "project_id", required=False),
        name=_text(row, "name"),
        category=_text(row, "category"),
        tags=[str(tag) for tag in _json(row, "tags", list, empty=[])],
        estimated_duration_minutes=_int(row, "duration_minutes"),
        priority=_int(row, "priority"),
        required=required_text == "true",
        required_date=_date(row, "required_date", required=False),
        preferred_dates=[date_.fromisoformat(str(day)) for day in _json(row, "preferred_dates", list, empty=[])],
        preferred_time_window=window,
        dependency_ids=[uuid.UUID(str(value)) for value in _json(row, "dependency_ids", list, empty=[])],
        deadline=_instant(row, "deadline", required=False),
        recurrence=RecurrenceSpec.model_validate(recurrence) if recurrence is not None else None,
        **_audit(row),
    )
    planned = task_planned_date(task)
    given = _text(row, "date")
    if given and given != (planned.isoformat() if planned else ""):
        raise _RowError(
            f"date is {given!r} but required_date/preferred_dates give {planned.isoformat() if planned else 'none'}; "
            "date is derived (edit required_date or preferred_dates)"
        )
    return task


def _parse_fixed_block(row: dict[str, str]) -> FixedBlock:
    day = _date(row, "date", required=True)
    tz_name = _text(row, "timezone")
    start = _instant(row, "start_utc", required=True)
    end = _instant(row, "end_utc", required=True)
    block = FixedBlock(
        id=_record_id(row), label=_text(row, "name"), category=_text(row, "category") or "fixed",
        planned_date=day, timezone=tz_name, planned_start=start, planned_end=end, **_audit(row),
    )
    _check_interval(row, start, end, day, tz_name)
    return block


def _parse_placement(row: dict[str, str]) -> ScheduledTask:
    day = _date(row, "date", required=True)
    tz_name = _text(row, "timezone")
    start = _instant(row, "start_utc", required=True)
    end = _instant(row, "end_utc", required=True)
    score_text = _text(row, "score") or "0"
    try:
        score = float(score_text)
    except ValueError as error:
        raise _RowError(f"score must be a number, got {score_text!r}") from error
    placement = ScheduledTask(
        id=_record_id(row), task_id=_uuid(row, "task_id", required=True), planned_date=day, timezone=tz_name,
        planned_start=start, planned_end=end, score=score,
        optimization_metadata=_json(row, "optimization_metadata", dict, empty={}), **_audit(row),
    )
    _check_interval(row, start, end, day, tz_name)
    return placement


_PARSERS = {
    "project": _parse_project,
    "task": _parse_task,
    "fixed_block": _parse_fixed_block,
    "placement": _parse_placement,
}
