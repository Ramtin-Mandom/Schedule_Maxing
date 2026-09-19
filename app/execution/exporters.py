"""
exporters.py

Write raw TaskExecution history to CSV or JSON, for the user's own local
export of their execution data. Mirrors the pattern in
app/productivity/exporters.py: both functions are pure over an
already-fetched list (obtained via ExecutionService.list_executions, never
by querying the repository directly from a caller), no DB access here.

These export the user's own data at their explicit request, so there is no
redaction of notes or other fields -- that would defeat the purpose of an
export. (Compare: application logs must never include notes/personal data;
this module is not a log.)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.execution.models import TaskExecution

_FIELDS = (
    "id",
    "task_name",
    "category",
    "tag",
    "planned_date",
    "planned_start",
    "planned_end",
    "planned_duration",
    "priority",
    "status",
    "created_at",
    "updated_at",
    "actual_active_duration_minutes",
    "duration_variance_minutes",
    "start_delay_minutes",
    "focus_rating",
    "energy_rating",
    "interruption_count",
    "note",
)


def export_executions_to_csv(executions: list[TaskExecution], path: str | Path) -> None:
    """Write one row per execution to CSV."""
    with Path(path).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=_FIELDS)
        writer.writeheader()
        for execution in executions:
            row = execution.model_dump(mode="json")
            writer.writerow({field: row[field] for field in _FIELDS})


def export_executions_to_json(executions: list[TaskExecution], path: str | Path) -> None:
    """Write the full execution list as pretty-printed JSON."""
    payload = [execution.model_dump(mode="json") for execution in executions]
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
