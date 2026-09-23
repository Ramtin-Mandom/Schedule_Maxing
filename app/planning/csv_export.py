"""
app/planning/csv_export.py

CSV export of *stored* planning data, read from SQLite through
PlanningService (never from widget state or an in-memory schedule). Export
only reads: it never writes to the database.

Contract ("stored planning CSV", format version 2 -- the canonical,
identity-preserving planning CSV; app/planning/csv_canonical.py imports
it back). One header row, then one row per record, standard CSV quoting
(csv module, RFC 4180 style; values may contain commas, quotes, and
newlines). The first 24 columns are exactly version 1's, in the same
order, so readers of version 1 keep working; version 2 appends seven.

    record_type     "project", "task", "fixed_block", or "placement"
    id              the record's UUID
    task_id         placement: the UUID of its task (empty otherwise)
    date            task: planned date (required_date, else earliest
                    preferred date; empty if undated) -- derived;
                    fixed_block / placement: its planned local date (YYYY-MM-DD)
    timezone        fixed_block / placement: IANA timezone
    name            project name / task name / fixed-block label /
                    placement's task name (derived, informational)
    category        task / fixed_block: category; placement: its task's (derived)
    tags            task: JSON array of tags, in order
    start_local     fixed_block / placement: local start HH:MM on `date` (derived)
    end_local       fixed_block / placement: local end HH:MM, 24:00 = next midnight (derived)
    start_utc       fixed_block / placement: exact start, ISO 8601 in UTC (authoritative)
    end_utc         fixed_block / placement: exact end, ISO 8601 in UTC (authoritative)
    duration_minutes  task: estimate (authoritative); fixed_block / placement: exact length (derived)
    priority        task: 1-10
    required        task: true/false
    required_date   task: YYYY-MM-DD or empty
    preferred_dates task: JSON array of YYYY-MM-DD
    preferred_window  task: "HH:MM-HH:MM" local, or empty
    deadline        task: ISO 8601 with its original offset, or empty
    dependency_ids  task: JSON array of UUIDs
    score           placement: optimizer score (Python float repr, exact)
    version         every record: its local edit revision
    created_at      every record: ISO 8601 UTC
    updated_at      every record: ISO 8601 UTC
    --- appended in format version 2 ---
    format_version  "2" on every row
    user_id         every record: owner UUID, or empty for a local ownerless record
    project_id      task: its project's UUID, or empty
    description     project: description (empty = none)
    recurrence      task: JSON object of its recurrence rule (model-only), or empty
    optimization_metadata  placement: JSON object
    deleted_at      every record: ISO 8601 UTC tombstone time, or empty for a live record

Rows are ordered: projects and tasks by (created_at, id), then fixed
blocks and placements by (date, start, id). Instants are exact to the
microsecond -- never rounded to a time grid. Fixed-block/placement instants
are written in UTC (the instant, its local date and timezone are kept; the
UTC offset a stored instant happened to be written with is not).

Deleted records (tombstones) are only written with include_deleted=True,
e.g. for a complete backup whose deletions should round-trip. A range
export writes the tasks planned in the range (RangeScope.PLANNED, undated
tasks included), the projects those tasks reference, and the fixed blocks
and placements dated in it; without a range, everything stored.

The legacy exports keep their own, lossy contracts: the CLI's `time,task`
half-hour-block CSV cannot represent tasks shorter than 30 minutes or
off-grid boundaries, and carries no ids.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timezone
from pathlib import Path

from app.planning.application import PlanningService, task_planned_date
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.time import local_minutes, minutes_to_hhmm

FORMAT_VERSION = 2

COLUMNS_V1 = (
    "record_type", "id", "task_id", "date", "timezone", "name", "category", "tags",
    "start_local", "end_local", "start_utc", "end_utc", "duration_minutes", "priority",
    "required", "required_date", "preferred_dates", "preferred_window", "deadline",
    "dependency_ids", "score", "version", "created_at", "updated_at",
)

COLUMNS = COLUMNS_V1 + (
    "format_version", "user_id", "project_id", "description", "recurrence", "optimization_metadata", "deleted_at",
)


@dataclass(frozen=True)
class PlanningExportResult:
    path: Path
    tasks: int
    fixed_blocks: int
    placements: int
    projects: int = 0


def export_planning_csv(
    service: PlanningService,
    path: str | Path,
    *,
    start_date: date_ | None = None,
    end_date: date_ | None = None,
    include_deleted: bool = False,
) -> PlanningExportResult:
    """
    Write stored planning data to `path`. With a date range: the tasks
    planned in it (RangeScope.PLANNED, undated tasks included), the projects
    they reference, and the fixed blocks and placements dated in it; without
    one: everything stored. include_deleted also writes tombstones.
    """
    if (start_date is None) != (end_date is None):
        raise ValueError("pass both start_date and end_date, or neither")

    with service.transaction():  # one consistent snapshot; nothing is written
        if start_date is None:
            projects = service.list_projects(include_deleted=include_deleted)
            tasks = service.list_tasks(include_deleted=include_deleted)
            blocks = service.list_fixed_blocks(include_deleted=include_deleted)
            placements = service.list_placements(include_deleted=include_deleted)
        else:
            tasks = service.tasks_planned_in_range(start_date, end_date, include_deleted=include_deleted)
            referenced = {task.project_id for task in tasks if task.project_id is not None}
            projects = [
                project for project in service.list_projects(include_deleted=include_deleted) if project.id in referenced
            ]
            blocks = service.list_fixed_blocks(start_date, end_date, include_deleted=include_deleted)
            placements = service.list_placements(start_date, end_date, include_deleted=include_deleted)
        placement_tasks = service.get_tasks_including_deleted(placement.task_id for placement in placements)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for project in projects:
            writer.writerow(_project_row(project))
        for task in tasks:
            writer.writerow(_task_row(task))
        for block in blocks:
            writer.writerow(_block_row(block))
        for placement in placements:
            writer.writerow(_placement_row(placement, placement_tasks.get(placement.task_id)))

    return PlanningExportResult(
        path=target, tasks=len(tasks), fixed_blocks=len(blocks), placements=len(placements), projects=len(projects)
    )


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _record(record) -> dict[str, str]:
    """The metadata every record type carries."""
    return {
        "id": str(record.id),
        "version": str(record.version),
        "created_at": _utc(record.created_at),
        "updated_at": _utc(record.updated_at),
        "format_version": str(FORMAT_VERSION),
        "user_id": str(record.user_id) if record.user_id else "",
        "deleted_at": _utc(record.deleted_at) if record.deleted_at else "",
    }


def _interval(start: datetime, end: datetime, day: date_, tz_name: str) -> dict[str, str]:
    end_minute = local_minutes(end, day, tz_name)
    return {
        "start_local": minutes_to_hhmm(local_minutes(start, day, tz_name)),
        "end_local": minutes_to_hhmm(end_minute),
        "start_utc": _utc(start),
        "end_utc": _utc(end),
        "duration_minutes": f"{(end - start).total_seconds() / 60:g}",
    }


def _project_row(project: Project) -> dict[str, str]:
    return {
        "record_type": "project",
        **_record(project),
        "name": project.name,
        "description": project.description or "",
    }


def _task_row(task: Task) -> dict[str, str]:
    planned = task_planned_date(task)
    window = task.preferred_time_window
    return {
        "record_type": "task",
        **_record(task),
        "date": planned.isoformat() if planned else "",
        "name": task.name,
        "category": task.category,
        "tags": json.dumps(task.tags),
        "duration_minutes": str(task.estimated_duration_minutes),
        "priority": str(task.priority),
        "required": "true" if task.required else "false",
        "required_date": task.required_date.isoformat() if task.required_date else "",
        "preferred_dates": json.dumps([day.isoformat() for day in task.preferred_dates]),
        "preferred_window": (
            f"{minutes_to_hhmm(window.start_minute)}-{minutes_to_hhmm(window.end_minute)}" if window else ""
        ),
        "deadline": task.deadline.isoformat() if task.deadline else "",
        "dependency_ids": json.dumps([str(dependency) for dependency in task.dependency_ids]),
        "project_id": str(task.project_id) if task.project_id else "",
        "recurrence": json.dumps(task.recurrence.model_dump(mode="json"), sort_keys=True) if task.recurrence else "",
    }


def _block_row(block: FixedBlock) -> dict[str, str]:
    return {
        "record_type": "fixed_block",
        **_record(block),
        "date": block.planned_date.isoformat(),
        "timezone": block.timezone,
        "name": block.label,
        "category": block.category,
        **_interval(block.planned_start, block.planned_end, block.planned_date, block.timezone),
    }


def _placement_row(placement: ScheduledTask, task: Task | None) -> dict[str, str]:
    return {
        "record_type": "placement",
        **_record(placement),
        "task_id": str(placement.task_id),
        "date": placement.planned_date.isoformat(),
        "timezone": placement.timezone,
        "name": task.name if task else "",
        "category": task.category if task else "",
        **_interval(placement.planned_start, placement.planned_end, placement.planned_date, placement.timezone),
        "score": repr(placement.score),
        "optimization_metadata": json.dumps(placement.optimization_metadata, sort_keys=True),
    }
