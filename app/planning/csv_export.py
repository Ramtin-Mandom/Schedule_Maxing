"""
app/planning/csv_export.py

CSV export of *stored* planning data, read from SQLite through
PlanningService (never from widget state or an in-memory schedule). Export
only reads: it never writes to the database.

Contract ("stored planning CSV", version 1) -- one header row, then one row
per entity, standard CSV quoting (csv module, RFC 4180 style; values may
contain commas, quotes, and newlines):

    record_type     "task", "fixed_block", or "placement"
    id              the entity's UUID
    task_id         placement: the UUID of its task (empty otherwise)
    date            task: planned date (required_date, else earliest
                    preferred date; empty if undated); fixed_block /
                    placement: its planned local date (YYYY-MM-DD)
    timezone        fixed_block / placement: IANA timezone
    name            task name / fixed-block label / placement's task name
    category        task / placement: category
    tags            task: JSON array of tags, in order
    start_local     fixed_block / placement: local start HH:MM on `date`
    end_local       fixed_block / placement: local end HH:MM (24:00 = next midnight)
    start_utc       fixed_block / placement: exact start, ISO 8601 UTC
    end_utc         fixed_block / placement: exact end, ISO 8601 UTC
    duration_minutes  task: estimate; fixed_block / placement: exact length
    priority        task: 1-10
    required        task: true/false
    required_date   task: YYYY-MM-DD or empty
    preferred_dates task: JSON array of YYYY-MM-DD
    preferred_window  task: "HH:MM-HH:MM" local, or empty
    deadline        task: ISO 8601 with its original offset, or empty
    dependency_ids  task: JSON array of UUIDs
    score           placement: optimizer score
    version         task / placement: version
    created_at      task / placement: ISO 8601 UTC
    updated_at      task / placement: ISO 8601 UTC

Rows are ordered: tasks by (created_at, id), then fixed blocks and
placements by (date, start, id). Instants are exact to the microsecond --
never rounded to a time grid.

This export is for reading, backup inspection, and analysis. It is not an
identity-preserving import format (CSV import mints new ids, see
app/planning/csv_import.py). The legacy exports keep their own, lossy
contracts: the CLI's `time,task` half-hour-block CSV cannot represent
tasks shorter than 30 minutes or off-grid boundaries, and carries no ids.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timezone
from pathlib import Path

from app.planning.application import PlanningService, RangeScope, task_planned_date
from app.planning.models import FixedBlock, ScheduledTask, Task
from app.planning.time import local_minutes, minutes_to_hhmm

FORMAT_VERSION = 1

COLUMNS = (
    "record_type", "id", "task_id", "date", "timezone", "name", "category", "tags",
    "start_local", "end_local", "start_utc", "end_utc", "duration_minutes", "priority",
    "required", "required_date", "preferred_dates", "preferred_window", "deadline",
    "dependency_ids", "score", "version", "created_at", "updated_at",
)


@dataclass(frozen=True)
class PlanningExportResult:
    path: Path
    tasks: int
    fixed_blocks: int
    placements: int


def export_planning_csv(
    service: PlanningService,
    path: str | Path,
    *,
    start_date: date_ | None = None,
    end_date: date_ | None = None,
) -> PlanningExportResult:
    """
    Write stored planning data to `path`. With a date range: the tasks
    planned in it (RangeScope.PLANNED, undated tasks included) and the fixed
    blocks and placements dated in it; without one: everything stored.
    """
    if (start_date is None) != (end_date is None):
        raise ValueError("pass both start_date and end_date, or neither")

    with service.transaction():  # one consistent snapshot; nothing is written
        if start_date is None:
            tasks = service.list_tasks()
            blocks = service.list_fixed_blocks()
            placements = service.list_placements()
        else:
            tasks = service.load_range(start_date, end_date, scope=RangeScope.PLANNED).tasks.tasks.values()
            tasks = sorted(tasks, key=lambda task: (task.created_at, str(task.id)))
            blocks = service.list_fixed_blocks(start_date, end_date)
            placements = service.list_placements(start_date, end_date)
        placement_tasks = service.get_tasks(placement.task_id for placement in placements)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for task in tasks:
            writer.writerow(_task_row(task))
        for block in blocks:
            writer.writerow(_block_row(block))
        for placement in placements:
            writer.writerow(_placement_row(placement, placement_tasks.get(placement.task_id)))

    return PlanningExportResult(path=target, tasks=len(tasks), fixed_blocks=len(blocks), placements=len(placements))


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _interval(start: datetime, end: datetime, day: date_, tz_name: str) -> dict[str, str]:
    end_minute = local_minutes(end, day, tz_name)
    return {
        "start_local": minutes_to_hhmm(local_minutes(start, day, tz_name)),
        "end_local": minutes_to_hhmm(end_minute),
        "start_utc": _utc(start),
        "end_utc": _utc(end),
        "duration_minutes": f"{(end - start).total_seconds() / 60:g}",
    }


def _task_row(task: Task) -> dict[str, str]:
    planned = task_planned_date(task)
    window = task.preferred_time_window
    return {
        "record_type": "task",
        "id": str(task.id),
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
        "version": str(task.version),
        "created_at": _utc(task.created_at),
        "updated_at": _utc(task.updated_at),
    }


def _block_row(block: FixedBlock) -> dict[str, str]:
    return {
        "record_type": "fixed_block",
        "id": str(block.id),
        "date": block.planned_date.isoformat(),
        "timezone": block.timezone,
        "name": block.label,
        **_interval(block.planned_start, block.planned_end, block.planned_date, block.timezone),
    }


def _placement_row(placement: ScheduledTask, task: Task | None) -> dict[str, str]:
    return {
        "record_type": "placement",
        "id": str(placement.id),
        "task_id": str(placement.task_id),
        "date": placement.planned_date.isoformat(),
        "timezone": placement.timezone,
        "name": task.name if task else "",
        "category": task.category if task else "",
        **_interval(placement.planned_start, placement.planned_end, placement.planned_date, placement.timezone),
        "score": repr(placement.score),
        "version": str(placement.version),
        "created_at": _utc(placement.created_at),
        "updated_at": _utc(placement.updated_at),
    }
