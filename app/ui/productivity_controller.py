"""
productivity_controller.py

UI-facing wrapper around app.productivity.reporting.ProductivityService.
Like ExecutionController, this is the only thing app/ui widgets should call
into for productivity analysis, duration prediction, and history export --
never the service or repository directly.

Storage wording (Milestone 5): what the history section says and what its
reset does depend on where history lives -- `storage` is "device" (the
ownerless local workspace), "account" (a local workspace of a synchronized
account) or "server" (direct PostgreSQL) -- see storage_copy().
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.execution.errors import ExecutionError
from app.execution.exporters import export_executions_to_csv, export_executions_to_json
from app.productivity.buckets import TimeBucket
from app.productivity.filters import ObservationFilters
from app.productivity.prediction import DurationPrediction
from app.productivity.reporting import DurationPredictionComparison, ProductivityDashboard, ProductivityService
from app.productivity.schedule_cohort import ScheduleCohortReport
from app.productivity.tracker import TrackerFilters, TrackerReport
from app.planning.time import local_date_of
from app.ui.background import ControllerResult
from app.ui.execution_controller import ExecutionController
from app.ui.history_model import HistoryPage, build_history_page

ExportFormat = str  # "csv" or "json"


@dataclass(frozen=True)
class StorageCopy:
    """The history section's wording for the active storage mode (never claims more than the mode does)."""

    summary: str
    reset_button: str
    reset_title: str
    reset_message: str


_STORAGE_COPY = {
    "device": StorageCopy(
        summary="Execution history is stored on this device only (no account is in use).",
        reset_button="Delete history on this device...",
        reset_title="Delete Execution History On This Device",
        reset_message="This permanently deletes all execution history stored on this device (including work "
                      "sessions and feedback). It cannot be undone.\n\nContinue?",
    ),
    "account": StorageCopy(
        summary="Execution history is stored on this device and synchronized with your account.",
        reset_button="Delete history...",
        reset_title="Delete Execution History",
        reset_message="This deletes this workspace's execution history on this device now. The next "
                      "synchronization also deletes the history this account had synchronized from the server, "
                      "and your other devices receive those deletions.\n\nContinue?",
    ),
    "server": StorageCopy(
        summary="Execution history is stored in the server database (direct mode) for this account.",
        reset_button="Delete history...",
        reset_title="Delete Execution History",
        reset_message="This deletes every execution of this account in the server database (they are kept as "
                      "deleted records and disappear from every device and report).\n\nContinue?",
    ),
}


def task_tags(planning) -> list[str]:
    """The tags of every task of a PlanningController's workspace (none when the tasks cannot be read)."""
    result = planning.list_tasks()
    return [tag for task in result.value for tag in task.tags] if result.ok else []


class ProductivityController:
    def __init__(
        self,
        productivity_service: ProductivityService,
        execution_controller: ExecutionController,
        *,
        storage: str = "device",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        task_tags: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._service = productivity_service
        #: Reads the tags of the workspace's tasks (None: only the tags of the recorded history are known).
        self._task_tags = task_tags
        self._execution_controller = execution_controller
        self._storage = storage
        self._clock = clock

    def storage_copy(self) -> StorageCopy:
        return _STORAGE_COPY[self._storage]

    @property
    def reporting_timezone(self) -> str | None:
        return self._service.reporting_timezone

    def _last_days(self, days: int) -> tuple[date, date]:
        tz = self._service.reporting_timezone
        if tz is None:
            raise ValueError("a reporting timezone is required (the host timezone is never assumed).")
        today = local_date_of(self._clock(), tz)
        return today - timedelta(days=days - 1), today

    def schedule_cohort_for_last(self, days: int) -> ControllerResult[ScheduleCohortReport]:
        """The cohort of the last `days` local dates (today included) in the reporting timezone, as of now."""

        def op() -> ScheduleCohortReport:
            start, end = self._last_days(days)
            return self._service.build_schedule_cohort_report(start_date=start, end_date=end)

        return self._call(op)

    def history(self, days: int, *, status: str | None = None, category: str | None = None
                ) -> ControllerResult[HistoryPage]:
        """Browsable history of the last `days` local dates (read-only; see app/ui/history_model.py)."""

        def op() -> HistoryPage:
            start, end = self._last_days(days)
            history, report = self._service.schedule_history_and_report(start_date=start, end_date=end)
            return build_history_page(history, report, status=status, category=category)

        return self._call(op)

    def build_dashboard(self, filters: ObservationFilters | None = None) -> ControllerResult[ProductivityDashboard]:
        return self._call(lambda: self._service.build_dashboard(filters=filters))

    def build_tracker(self, range_days: int | None = None, filters: TrackerFilters | None = None
                      ) -> ControllerResult[TrackerReport]:
        """The tracker report (awards, averages, per-type and time views) as of now; read-only."""
        return self._call(lambda: self._service.build_tracker_report(range_days=range_days, filters=filters))

    def projects(self) -> ControllerResult[list]:
        """The workspace's live projects, by name (the Project section's choices)."""
        return self._call(self._service.projects)

    def build_project_points(self, project_id, range_days: int | None = None):
        """One project's collected points in the date range (app/productivity/project_stats.py); read-only."""
        return self._call(lambda: self._service.build_project_points(project_id, range_days=range_days))

    def used_tags(self) -> list[str]:
        """Every tag on the workspace's tasks, scheduled or not (empty when they cannot be read)."""
        if self._task_tags is None:
            return []
        try:
            return sorted(set(self._task_tags()))
        except Exception:  # noqa: BLE001 - filter choices only: the history's own tags are still offered
            return []

    def build_schedule_cohort_report(
        self, start_date: date, end_date: date, *, timezone_name: str | None = None, as_of: datetime | None = None,
    ) -> ControllerResult[ScheduleCohortReport]:
        """The schedule-cohort report (read-only; the app's reporting timezone unless one is given)."""
        return self._call(lambda: self._service.build_schedule_cohort_report(
            start_date=start_date, end_date=end_date, timezone_name=timezone_name, as_of=as_of))

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

    def predict_duration_comparison(
        self,
        *,
        category: str,
        tag: str,
        priority: int,
        planned_start: int,
        original_estimate_minutes: float,
        filters: ObservationFilters | None = None,
        data_dir: str | None = None,
    ) -> ControllerResult[DurationPredictionComparison]:
        """Side-by-side median-vs-ML comparison for the same task; does not change which predictor is active."""
        return self._call(
            lambda: self._service.predict_duration_comparison(
                category=category,
                tag=tag,
                priority=priority,
                planned_start=planned_start,
                original_estimate_minutes=original_estimate_minutes,
                filters=filters,
                data_dir=data_dir,
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
        """Delete this workspace's execution history as storage_copy() describes. The UI must confirm first."""
        return self._execution_controller.reset_all_history()

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except ExecutionError as error:  # includes direct-storage failures (app/persistence/errors.py): safe messages
            return ControllerResult.failure(str(error), error)
        except Exception as error:  # noqa: BLE001 - last-resort safety net so DB/unexpected errors never crash the UI
            return ControllerResult.failure(f"Unexpected error: {error}", error)
