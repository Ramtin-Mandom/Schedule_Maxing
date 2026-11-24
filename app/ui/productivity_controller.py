"""Desktop controller for the Productivity tracker's points, projects and filters."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from app.execution.errors import ExecutionError
from app.productivity.reporting import ProductivityService
from app.productivity.tracker import TrackerFilters, TrackerReport
from app.ui.background import ControllerResult


def task_tags(planning) -> list[str]:
    """The tags of every task of a PlanningController's workspace (none when the tasks cannot be read)."""
    result = planning.list_tasks()
    return [tag for task in result.value for tag in task.tags] if result.ok else []


class ProductivityController:
    def __init__(
        self,
        productivity_service: ProductivityService,
        *,
        task_tags: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._service = productivity_service
        self._task_tags = task_tags

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

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except ExecutionError as error:  # includes direct-storage failures (app/persistence/errors.py): safe messages
            return ControllerResult.failure(str(error), error)
        except Exception as error:  # noqa: BLE001 - last-resort safety net so DB/unexpected errors never crash the UI
            return ControllerResult.failure(f"Unexpected error: {error}", error)
