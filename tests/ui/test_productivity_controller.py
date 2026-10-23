"""Tests for app/ui/productivity_controller.py: filter pass-through, empty/
low-data/sufficient-data dashboards, export, reset, and that duration
suggestions never mutate execution state (they're read-only predictions).

None of these instantiate Tk/CustomTkinter -- pure controller/service logic.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket
from app.productivity.filters import ObservationFilters
from app.productivity.stats import EvidenceLevel
from app.ui.execution_controller import ExecutionController
from app.ui.productivity_controller import ProductivityController
from tests.productivity.fixtures import build_synthetic_dataset
from tests.ui.conftest import make_snapshot_kwargs


# ----------------------------------------------------------------------
# Empty / low-data / sufficient-data dashboards
# ----------------------------------------------------------------------


def test_dashboard_on_empty_history_is_safe(productivity_controller: ProductivityController) -> None:
    result = productivity_controller.build_dashboard()

    assert result.ok
    dashboard = result.value
    assert dashboard.observation_count == 0
    assert dashboard.by_category == {}
    assert dashboard.global_stats.completion_rate is None
    assert dashboard.insights == []


def test_dashboard_with_low_data_reports_insufficient_evidence(
    productivity_controller: ProductivityController, execution_controller: ExecutionController
) -> None:
    execution_controller.get_or_create_execution(**make_snapshot_kwargs())  # just 1 record

    result = productivity_controller.build_dashboard()

    assert result.ok
    dashboard = result.value
    assert dashboard.observation_count == 1
    assert dashboard.global_stats.evidence_level == EvidenceLevel.INSUFFICIENT


def test_dashboard_with_sufficient_data_shows_evidence_backed_stats(
    productivity_controller: ProductivityController, repository: ExecutionRepository
) -> None:
    build_synthetic_dataset(repository)

    result = productivity_controller.build_dashboard()

    assert result.ok
    dashboard = result.value
    assert dashboard.observation_count > 0
    assert set(dashboard.by_category) == {"study", "exercise", "errand"}
    assert len(dashboard.insights) > 0


# ----------------------------------------------------------------------
# Filter pass-through
# ----------------------------------------------------------------------


def test_dashboard_filters_pass_through_to_service(
    productivity_controller: ProductivityController, repository: ExecutionRepository
) -> None:
    build_synthetic_dataset(repository)

    result = productivity_controller.build_dashboard(filters=ObservationFilters(category="exercise"))

    assert result.ok
    assert set(result.value.by_category) == {"exercise"}


# ----------------------------------------------------------------------
# Duration suggestion: read-only, never mutates execution/task state
# ----------------------------------------------------------------------


def test_predict_duration_does_not_create_or_modify_any_execution(
    productivity_controller: ProductivityController, repository: ExecutionRepository
) -> None:
    build_synthetic_dataset(repository)
    executions_before = {execution.id: execution for execution in repository.list_executions()}

    result = productivity_controller.predict_duration(
        category="study", time_bucket=TimeBucket.MORNING, original_estimate_minutes=45
    )

    assert result.ok
    prediction = result.value
    assert prediction.original_estimate_minutes == 45  # the caller's own estimate is echoed back, untouched
    executions_after = {execution.id: execution for execution in repository.list_executions()}
    assert executions_before == executions_after  # no execution rows created, changed, or removed


def test_predict_duration_with_insufficient_history_says_so(
    productivity_controller: ProductivityController,
) -> None:
    result = productivity_controller.predict_duration(
        category="study", time_bucket=TimeBucket.MORNING, original_estimate_minutes=45
    )

    assert result.ok
    prediction = result.value
    assert prediction.evidence_level == EvidenceLevel.INSUFFICIENT
    assert prediction.predicted_duration_minutes == 45  # falls back to the caller's own estimate
    assert "not enough" in prediction.explanation.lower() or "not enough matching history" in prediction.explanation.lower()


# ----------------------------------------------------------------------
# Export / reset
# ----------------------------------------------------------------------


def test_export_execution_history_to_csv(
    productivity_controller: ProductivityController,
    execution_controller: ExecutionController,
    tmp_path: Path,
) -> None:
    execution_controller.get_or_create_execution(**make_snapshot_kwargs())
    output_path = tmp_path / "history.csv"

    result = productivity_controller.export_execution_history(output_path, "csv")

    assert result.ok
    assert result.value == 1
    with output_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1


def test_export_execution_history_to_json(
    productivity_controller: ProductivityController,
    execution_controller: ExecutionController,
    tmp_path: Path,
) -> None:
    execution_controller.get_or_create_execution(**make_snapshot_kwargs())
    output_path = tmp_path / "history.json"

    result = productivity_controller.export_execution_history(output_path, "json")

    assert result.ok
    loaded = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(loaded) == 1


def test_export_empty_history_produces_empty_file(
    productivity_controller: ProductivityController, tmp_path: Path
) -> None:
    output_path = tmp_path / "history.csv"

    result = productivity_controller.export_execution_history(output_path, "csv")

    assert result.ok
    assert result.value == 0


def test_reset_all_history_via_controller(
    productivity_controller: ProductivityController,
    execution_controller: ExecutionController,
) -> None:
    execution_controller.get_or_create_execution(**make_snapshot_kwargs())

    result = productivity_controller.reset_all_history()

    assert result.ok
    assert result.value == 1
    assert productivity_controller.build_dashboard().value.observation_count == 0
