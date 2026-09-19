"""Tests for app/execution/exporters.py: raw execution-history export."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.execution.exporters import export_executions_to_csv, export_executions_to_json
from app.execution.models import ExecutionStatus, TaskExecution

TIMESTAMP = "2024-01-01T09:00:00+00:00"


def _make_execution(**overrides: object) -> TaskExecution:
    defaults = dict(
        id="exec-1",
        task_name="Study Math",
        category="study",
        tag="math",
        planned_date=1,
        planned_start=540,
        planned_end=660,
        planned_duration=120,
        priority=8,
        status=ExecutionStatus.COMPLETED,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
        actual_active_duration_minutes=65.0,
        note="a private note",
    )
    defaults.update(overrides)
    return TaskExecution(**defaults)


def test_export_executions_to_csv(tmp_path: Path) -> None:
    executions = [_make_execution(id="exec-1"), _make_execution(id="exec-2", status=ExecutionStatus.SKIPPED)]
    output_path = tmp_path / "history.csv"

    export_executions_to_csv(executions, output_path)

    with output_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert [row["id"] for row in rows] == ["exec-1", "exec-2"]
    # An explicit user export includes their own notes (not a log) -- no redaction.
    assert rows[0]["note"] == "a private note"


def test_export_executions_to_json(tmp_path: Path) -> None:
    executions = [_make_execution()]
    output_path = tmp_path / "history.json"

    export_executions_to_json(executions, output_path)

    loaded = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(loaded) == 1
    assert loaded[0]["id"] == "exec-1"
    assert loaded[0]["actual_active_duration_minutes"] == 65.0


def test_export_empty_history(tmp_path: Path) -> None:
    csv_path = tmp_path / "empty.csv"
    json_path = tmp_path / "empty.json"

    export_executions_to_csv([], csv_path)
    export_executions_to_json([], json_path)

    with csv_path.open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert rows == []
    assert json.loads(json_path.read_text(encoding="utf-8")) == []
