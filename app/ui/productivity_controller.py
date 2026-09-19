"""
productivity_controller.py

UI-facing wrapper around app.productivity.reporting.ProductivityService.
Like ExecutionController, this is the only thing app/ui widgets should call
into for productivity analysis, duration prediction, and history export --
never the service or repository directly.
"""

from __future__ import annotations

from pathlib import Path

from app.execution.exporters import export_executions_to_csv, export_executions_to_json
from app.productivity.buckets import TimeBucket
from app.productivity.filters import ObservationFilters
from app.productivity.prediction import DurationPrediction
from app.productivity.reporting import ProductivityDashboard, ProductivityService
from app.ui.background import ControllerResult
from app.ui.execution_controller import ExecutionController

ExportFormat = str  # "csv" or "json"


class ProductivityController:
    def __init__(self, productivity_service: ProductivityService, execution_controller: ExecutionController) -> None:
        self._service = productivity_service
        self._execution_controller = execution_controller

    def build_dashboard(self, filters: ObservationFilters | None = None) -> ControllerResult[ProductivityDashboard]:
        return self._call(lambda: self._service.build_dashboard(filters=filters))

    def predict_duration(
        self,
        *,
        category: str,
        time_bucket: TimeBucket,
        original_estimate_minutes: float,
        filters: ObservationFilters | None = None,
    ) -> ControllerResult[DurationPrediction]:
        return self._call(
            lambda: self._service.predict_duration(
                category=category,
                time_bucket=time_bucket,
                original_estimate_minutes=original_estimate_minutes,
                filters=filters,
            )
        )

    def export_execution_history(self, path: str | Path, export_format: ExportFormat) -> ControllerResult[int]:
        """Export the user's raw execution history to CSV or JSON at `path`. Returns the row count exported."""
        executions_result = self._execution_controller.list_executions()
        if not executions_result.ok:
            return ControllerResult.failure(executions_result.error)

        executions = executions_result.value

        def do_export() -> int:
            if export_format == "json":
                export_executions_to_json(executions, path)
            else:
                export_executions_to_csv(executions, path)
            return len(executions)

        return self._call(do_export)

    def reset_all_history(self) -> ControllerResult[int]:
        """Permanently delete all local execution history. The caller (UI) must confirm with the user first."""
        return self._execution_controller.reset_all_history()

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except Exception as error:  # noqa: BLE001 - last-resort safety net so DB/unexpected errors never crash the UI
            return ControllerResult.failure(f"Unexpected error: {error}")
