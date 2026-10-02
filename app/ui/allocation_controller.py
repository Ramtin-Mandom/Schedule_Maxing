"""
app/ui/allocation_controller.py

The Tk-free presenter of the desktop Allocation Planning page (Milestone 4,
Prompt 6).

**Allocation assigns dates, not times.** For a bounded week or month, the
shared allocator (workflow.preview_allocation, over persisted inputs) decides
which date each task goes to. It reports:

- the free minutes left on each date;
- which tasks got no date, with the service's reason, turned into words.

No intraday placement is made and nothing is saved: the result is a preview.
It can be recomputed at any time (for example after a reopen) and carries the
fingerprint of the inputs it was computed from. When the persisted inputs
change, the preview is marked out of date (is_stale).

- "Not found by this pass" is not called impossible. A reason is described
  as proven only when the service says so (proven_infeasible).
- An assignment is never turned into a task's required_date.
- **Scheduling one date from a preview** (schedule_date) generates exactly
  that date. It uses the preview's range for allocation and passes the
  preview's fingerprint as the precondition, so a preview that is no longer
  true is refused (StaleInputsError) rather than silently re-planned. Other
  dates are not optimized.
- **Filtering** by project only changes what is shown (filter_view).
  Allocation always uses every task, fixed block and dependency of the range.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date as date_
from datetime import datetime
from zoneinfo import ZoneInfo

from app.planning.allocation import AllocationReasonCode
from app.planning.application import RangeScope, task_planned_date
from app.planning.errors import RegenerationRequiredError, StaleInputsError
from app.planning.workflow import AllocationPreview, Freshness, GenerationMode
from app.ui.background import ControllerResult
from app.ui.calendar_model import Period
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import day_label
from app.ui.time_fields import format_clock, format_duration

#: The allocator's reason codes, in words (the service's own explanation is shown beside them).
REASON_TEXT: dict[AllocationReasonCode, str] = {
    AllocationReasonCode.CAPACITY_EXCEEDED: "Not enough free time on the possible dates",
    AllocationReasonCode.REQUIRED_DATE_CONFLICT: "Its required date is outside this range or unavailable",
    AllocationReasonCode.DEADLINE_INFEASIBLE: "Its deadline comes before any possible date",
    AllocationReasonCode.DEPENDENCY_UNRESOLVED: "A task it depends on is missing or cannot be placed first",
    AllocationReasonCode.BLOCKED_BY_UNALLOCATED_DEPENDENCY: "A task it depends on got no date",
    AllocationReasonCode.NO_FEASIBLE_DATE: "No date in this range fits it",
}


@dataclass(frozen=True)
class AllocatedTask:
    task_id: uuid.UUID
    name: str
    duration_minutes: int
    project_id: uuid.UUID | None
    project_name: str
    required: bool
    deadline_text: str
    planned_text: str


@dataclass(frozen=True)
class AllocationDay:
    date: date_
    tasks: list[AllocatedTask]
    #: Free minutes left after these assignments (fixed blocks already subtracted).
    free_minutes: int
    freshness: Freshness

    @property
    def free_text(self) -> str:
        return format_duration(self.free_minutes) if self.free_minutes > 0 else "none"


@dataclass(frozen=True)
class UnallocatedRow:
    task: AllocatedTask
    reason: str
    explanation: str
    #: True only when the service proved that no date in the range can work.
    proven: bool

    @property
    def certainty(self) -> str:
        return "No date in this range can work." if self.proven else \
            "This allocation pass found no date; that does not prove none exists."


@dataclass(frozen=True)
class AllocationView:
    period: Period
    fingerprint: str
    computed_at: datetime
    days: list[AllocationDay]
    unallocated: list[UnallocatedRow]
    projects: dict[uuid.UUID, str] = field(default_factory=dict)
    #: The project the view is filtered by (display only).
    project_filter: uuid.UUID | None = None

    @property
    def assigned_count(self) -> int:
        return sum(len(day.tasks) for day in self.days)

    def day(self, day: date_) -> AllocationDay | None:
        return next((item for item in self.days if item.date == day), None)


def filter_view(view: AllocationView, project_id: uuid.UUID | None) -> AllocationView:
    """The same allocation showing only one project's tasks (capacity and the allocation itself unchanged)."""
    if project_id is None:
        return replace(view, project_filter=None)
    return replace(
        view, project_filter=project_id,
        days=[replace(day, tasks=[task for task in day.tasks if task.project_id == project_id]) for day in view.days],
        unallocated=[row for row in view.unallocated if row.task.project_id == project_id])


class AllocationController:
    def __init__(self, planning: PlanningController, *, timezone: str, selected: date_, mode: str = "week",
                 today: Callable[[], date_] | None = None) -> None:
        self._planning = planning
        self.timezone = timezone
        self._today = today
        self.period = Period.for_date(mode, selected)

    # -- range -------------------------------------------------------------------------

    def today(self) -> date_:
        return self._today() if self._today is not None else datetime.now(ZoneInfo(self.timezone)).date()

    @property
    def selected_date(self) -> date_:
        return self.period.selected

    def set_mode(self, mode: str) -> None:
        self.period = Period.for_date(mode, self.period.selected)

    def shift(self, delta: int) -> None:
        self.period = self.period.shifted(delta)

    def go_today(self) -> None:
        self.period = Period.for_date(self.period.mode, self.today())

    def select(self, day: date_) -> None:
        self.period = replace(self.period, selected=day) if self.period.contains(day) \
            else Period.for_date(self.period.mode, day)

    # -- allocation --------------------------------------------------------------------

    def allocate(self, period: Period | None = None) -> ControllerResult[AllocationView]:
        """Allocate the period from persisted inputs (a preview: nothing is saved)."""
        period = period or self.period
        preview = self._planning.preview_allocation(period.start, period.end, scope=RangeScope.PLANNED)
        if not preview.ok:
            return ControllerResult.failure(preview.error, preview.cause)
        projects = self._planning.list_projects()
        if not projects.ok:
            return ControllerResult.failure(projects.error)
        return ControllerResult.success(self._view(period, preview.value, {p.id: p.name for p in projects.value}))

    def _view(self, period: Period, preview: AllocationPreview, projects: dict[uuid.UUID, str]) -> AllocationView:
        tasks = preview.inputs.planning_range.tasks.tasks
        allocation = preview.allocation

        def info(task_id: uuid.UUID) -> AllocatedTask:
            task = tasks.get(task_id)
            if task is None:
                return AllocatedTask(task_id, "(unknown task)", 0, None, "", False, "", "")
            deadline = ""
            if task.deadline is not None:
                local = task.deadline.astimezone(ZoneInfo(self.timezone))
                deadline = f"{day_label(local.date())} {format_clock(local.hour * 60 + local.minute)}"
            planned = task_planned_date(task)
            project = ""
            if task.project_id is not None:
                project = projects.get(task.project_id, f"(missing project {str(task.project_id)[:8]})")
            return AllocatedTask(task.id, task.name, task.estimated_duration_minutes, task.project_id, project,
                                 task.required, deadline, day_label(planned) if planned else "any date")

        order = {task_id: index for index, task_id in enumerate(preview.inputs.planning_range.task_ids)}
        by_day: dict[date_, list[AllocatedTask]] = {day: [] for day in period.dates}
        for task_id, day in sorted(allocation.assignments.items(), key=lambda item: order.get(item[0], 0)):
            by_day.setdefault(day, []).append(info(task_id))
        days = [AllocationDay(day, by_day[day], allocation.capacity_remaining.get(day, 0),
                              preview.freshness[day].status) for day in period.dates]
        unallocated = [UnallocatedRow(info(entry.task_id), REASON_TEXT.get(entry.reason_code, entry.reason_code.value),
                                      entry.explanation, entry.proven_infeasible)
                       for entry in allocation.unallocated]
        unallocated.sort(key=lambda row: (not row.task.required, order.get(row.task.task_id, 0)))
        return AllocationView(period=period, fingerprint=preview.fingerprint, computed_at=allocation.created_at,
                              days=days, unallocated=unallocated, projects=projects)

    def is_stale(self, view: AllocationView) -> ControllerResult[bool]:
        """True when the persisted inputs changed since `view` was computed."""
        current = self._planning.inputs_fingerprint(view.period.start, view.period.end, scope=RangeScope.PLANNED)
        if not current.ok:
            return ControllerResult.failure(current.error)
        return ControllerResult.success(current.value != view.fingerprint)

    def schedule_date(self, view: AllocationView, day: date_) -> ControllerResult[str]:
        """
        Generate exactly `day` from this allocation. The fingerprint is
        checked first: if anything changed since the preview, nothing is
        generated and the preview must be recalculated.
        """
        if not view.period.contains(day):
            return ControllerResult.failure(f"{day_label(day)} is not in this allocation's range.")
        freshness = self._planning.day_freshness([day])
        if not freshness.ok:
            return ControllerResult.failure(freshness.error)
        state = freshness.value[day]
        mode = GenerationMode.INCREMENTAL if state.status == Freshness.STALE and state.placements else GenerationMode.FULL
        result = self._planning.generate(
            view.period.start, view.period.end, generate_start=day, generate_end=day, mode=mode,
            expected_fingerprint=view.fingerprint, preserve_on_empty=True)
        if not result.ok:
            if isinstance(result.cause, StaleInputsError):
                return ControllerResult.failure("Something changed since this allocation was calculated. Nothing was "
                                                "scheduled; recalculate the allocation first.", result.cause)
            if isinstance(result.cause, RegenerationRequiredError):
                manual = any(problem.kept_as == "manual" for problem in result.cause.problems)
                return ControllerResult.failure(
                    "Saved work on this date no longer fits. Nothing was changed; open the day and "
                    + ("move or release the task you placed yourself, or choose Regenerate there." if manual
                       else "choose Regenerate there."), result.cause)
            return ControllerResult.failure(f"{result.error}\n\nNothing was saved.", result.cause)
        outcome = result.value
        if outcome.status == "already_current":
            return ControllerResult.success(f"{day_label(day)} is already current; nothing changed.")
        if outcome.status == "nothing_placed":
            return ControllerResult.success(f"Nothing could be placed on {day_label(day)}; its previous schedule "
                                            "was kept.")
        output = outcome.outputs[day]
        return ControllerResult.success(
            f"Scheduled {day_label(day)}: {len(output.placements)} task(s) placed"
            + (f", {len(output.unscheduled)} could not be placed" if output.unscheduled else "")
            + (f", {len(outcome.kept_elsewhere)} already planned on another date left there"
               if outcome.kept_elsewhere else "") + ".")
