"""
app/ui/day_outcomes.py

The Week/Month selected-day panel and historical colours, Tk-free
(tests/ui/test_day_outcomes.py): date-level aggregates of SCHEDULED work
(app/productivity/day_summary.py) and the bulk actions "All Tasks Complete"
/ "No Tasks Complete".

One state model: the Day page's Uncompleted | Tasks | Completed board, these
bulk actions and the colours all read and write the same thing -- each
placement's TaskExecution (app/execution/lifecycle.py). A bulk action is one
ExecutionController.set_outcomes call: every live placement of the date in
one transaction; tasks the scheduler did not place are not placements and
are never touched.

Reads are batched: a whole week or month grid is one planning range load
plus one execution query, never a read per date.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_

from app.execution.lifecycle import BulkOutcomeResult, TaskOutcome
from app.execution.models import TaskExecution
from app.planning.application import RangeScope
from app.planning.models import ScheduledTask, Task
from app.productivity.day_summary import DaySummary, summarize_range
from app.ui.background import ControllerResult
from app.ui.schedule_page_controller import ExecutablePlacement
from app.ui.task_status import StatusBoard, build_board


@dataclass(frozen=True)
class DayDetail:
    day: date_
    summary: DaySummary
    #: The date's scheduled tasks with their columns (the Day page's board, read-only here).
    board: StatusBoard


@dataclass(frozen=True)
class DayOutcomeRun:
    detail: DayDetail
    result: BulkOutcomeResult


class _Failure(Exception):
    def __init__(self, result: ControllerResult) -> None:
        super().__init__(result.error)
        self.result = result


def _unwrap(result: ControllerResult):
    if not result.ok:
        raise _Failure(result)
    return result.value


class DayOutcomeController:
    """Reads date aggregates and applies the bulk actions through the planning and execution controllers."""

    def __init__(self, planning, executions) -> None:
        self._planning = planning
        self._executions = executions

    # ------------------------------------------------------------------ reads

    def _load(self, start: date_, end: date_):
        planning_range = _unwrap(self._planning.load_range(start, end, scope=RangeScope.PLANNED))
        placements: list[ScheduledTask] = [p for day in sorted(planning_range.placements_by_date)
                                           for p in planning_range.placements_by_date[day]]
        tasks: dict[uuid.UUID, Task] = dict(planning_range.tasks.tasks)
        missing = [p.task_id for p in placements if p.task_id not in tasks]
        if missing:
            tasks.update(_unwrap(self._planning.get_tasks(missing)).tasks)
        executions: dict[uuid.UUID, TaskExecution] = (
            _unwrap(self._executions.executions_for_placements([p.id for p in placements])) if placements else {})
        return placements, tasks, executions

    def summaries(self, start: date_, end: date_) -> ControllerResult[dict[date_, DaySummary]]:
        """Every date's aggregates in [start, end], from one range load and one execution query."""
        try:
            placements, tasks, executions = self._load(start, end)
        except _Failure as failure:
            return failure.result
        days = [date_.fromordinal(ordinal) for ordinal in range(start.toordinal(), end.toordinal() + 1)]
        return ControllerResult.success(summarize_range(days, placements, tasks, executions))

    def detail(self, day: date_) -> ControllerResult[DayDetail]:
        """One date: its aggregates and its scheduled tasks with their columns."""
        try:
            placements, tasks, executions = self._load(day, day)
        except _Failure as failure:
            return failure.result
        return ControllerResult.success(self._detail(day, placements, tasks, executions))

    @staticmethod
    def _detail(day, placements, tasks, executions) -> DayDetail:
        summary = summarize_range([day], placements, tasks, executions)[day]
        items = [ExecutablePlacement(task=tasks[p.task_id], placement=p, label=tasks[p.task_id].name)
                 for p in placements if p.task_id in tasks]
        return DayDetail(day=day, summary=summary, board=build_board(day, items, executions))

    # ------------------------------------------------------------------ bulk actions

    def set_day(self, day: date_, outcome: TaskOutcome | str) -> ControllerResult[DayOutcomeRun]:
        """
        Every scheduled task of `day` to `outcome` ("All Tasks Complete" =
        completed, "No Tasks Complete" = uncompleted), in one transaction.
        On a failure nothing changed; the result carries the error.
        """
        try:
            placements, tasks, _ = self._load(day, day)
            items = [(tasks[p.task_id], p) for p in placements if p.task_id in tasks]
            result = _unwrap(self._executions.set_outcomes(items, TaskOutcome(outcome)))
            placements, tasks, executions = self._load(day, day)
        except _Failure as failure:
            return ControllerResult.failure(failure.result.error, failure.result.cause)
        return ControllerResult.success(DayOutcomeRun(self._detail(day, placements, tasks, executions), result))
