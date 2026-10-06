"""
app/ui/calendar_controller.py

The Tk-free presenter behind the desktop Week and Month pages (Milestone 4,
Prompt 5). It extends SchedulePageController, so the shared task form,
edits and removal are exactly Day's. The period's dates come from
app/ui/calendar_model.py: a Monday-first week, or a real calendar month
laid out in whole weeks.

Reading is bounded: one load reads one range (a week, or at most six weeks
of month grid) with a single load_range plus the persisted freshness of its
dates. load_for(period) takes an immutable Period, so the page can run it in
a background worker and drop a result for a period it no longer shows.

What a day shows:

- fixed blocks and saved placements at their actual times, in time order;
- then the tasks planned for it that are not on any schedule, in their
  input (creation) order, labelled "Not scheduled" -- never at invented
  times;
- a task planned here but scheduled on another date says where.

Past days are only marked (the page mutes them); nothing is hidden or
removed. Scheduling itself happens on the Day page (Open Day); Week and
Month have no Make Schedule of their own. Reset Week/Month uses the same
previewed, token-confirmed PlanningService reset as Reset Day, over the
period's own dates (a month's out-of-month cells are never included), and
refuses when tasks outside the period depend on tasks inside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import date as date_
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from app.planning.application import RangeScope, task_planned_date
from app.planning.time import local_minutes
from app.planning.workflow import Freshness
from app.ui.background import ControllerResult
from app.ui.calendar_model import Period, days_in_month, month_title, shift_month
from app.ui.day_controller import MINUTES_PER_DAY, ResetPlan, _interval_text, describe_reset
from app.productivity.day_summary import DayStatusClass, DaySummary, summarize_range
from app.ui.day_outcomes import DayOutcomeController
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import (
    PageSnapshot,
    RowRef,
    SchedulePageController,
    _Failure,
    day_label,
    display_namer,
)

ItemKind = Literal["fixed", "scheduled", "stale", "unscheduled", "elsewhere"]
_FRESHNESS_LABELS = {Freshness.CURRENT: "Current", Freshness.STALE: "Out of date", Freshness.NONE: ""}


@dataclass(frozen=True)
class CalendarItem:
    kind: ItemKind
    name: str
    category: str
    ref: RowRef
    #: Local minutes of a timed item (fixed block or placement); None for a task without a placement here.
    start_minute: int | None = None
    end_minute: int | None = None
    time_text: str = ""
    #: For "elsewhere": the dates it is scheduled on.
    elsewhere: tuple[date_, ...] = ()

    @property
    def timed(self) -> bool:
        return self.start_minute is not None

    @property
    def text(self) -> str:
        """One ordered line of the day's details."""
        if self.kind == "fixed":
            return f"{self.time_text}  {self.name} (fixed)"
        if self.kind in ("scheduled", "stale"):
            return f"{self.time_text}  {self.name}" + (" (out of date)" if self.kind == "stale" else "")
        if self.kind == "elsewhere":
            return f"{self.name} — scheduled on " + ", ".join(day_label(day) for day in self.elsewhere)
        return f"{self.name} — not scheduled"


@dataclass(frozen=True)
class CalendarDay:
    date: date_
    #: False for a month grid's days of the neighbouring months.
    in_period: bool
    is_past: bool
    is_today: bool
    #: Timed items in time order, then the rest in input order.
    items: list[CalendarItem] = field(default_factory=list)
    freshness: Freshness = Freshness.NONE
    #: The date's aggregates of scheduled work (None when execution data is not available to this page).
    summary: DaySummary | None = None

    @property
    def status_class(self) -> DayStatusClass | None:
        """The historical classification -- only for a past date of the shown period (today/future: None)."""
        if not (self.is_past and self.in_period) or self.summary is None:
            return None
        return self.summary.status_class

    @property
    def timed(self) -> list[CalendarItem]:
        return [item for item in self.items if item.timed]

    @property
    def untimed(self) -> list[CalendarItem]:
        return [item for item in self.items if not item.timed]

    @property
    def freshness_label(self) -> str:
        return _FRESHNESS_LABELS[self.freshness]


@dataclass(frozen=True)
class CalendarSnapshot(PageSnapshot):
    period: Period | None = None
    today: date_ | None = None
    days: list[CalendarDay] = field(default_factory=list)
    #: Tasks without any date (shown on every Day page's available list, counted here).
    undated_count: int = 0
    projects: dict[uuid.UUID, str] = field(default_factory=dict)
    task_projects: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)

    def day(self, day: date_) -> CalendarDay | None:
        return next((cell for cell in self.days if cell.date == day), None)

    def filtered(self, project_id: uuid.UUID | None) -> CalendarSnapshot:
        """Display filter only: all fixed blocks remain visible; generation inputs are untouched."""
        if project_id is None:
            return self

        def visible(ref):
            return ref.kind == "block" or self.task_projects.get(ref.id) == project_id

        return replace(self, rows=[row for row in self.rows if visible(row.ref)],
                       days=[replace(day, items=[item for item in day.items if visible(item.ref)]) for day in self.days])


class CalendarController(SchedulePageController):
    """One Week or Month page (see the module docstring)."""

    def __init__(self, planning: PlanningController, *, mode: str, selected: date_, timezone: str,
                 today: Callable[[], date_] | None = None, executions=None) -> None:
        self._period = Period.for_date(mode, selected)
        super().__init__(planning, number_of_days=len(self._period.dates), anchor_date=self._period.start,
                         timezone=timezone)
        self.mode = mode
        self._today = today
        self._executions = executions
        #: The selected-day panel's reads and bulk actions (None without execution tracking).
        self.outcomes = (DayOutcomeController(planning, executions, timezone=timezone)
                         if executions is not None else None)

    # ------------------------------------------------------------------
    # Period and selection
    # ------------------------------------------------------------------

    @property
    def period(self) -> Period:
        return self._period

    @property
    def selected_date(self) -> date_:
        return self._period.selected

    def today(self) -> date_:
        if self._today is not None:
            return self._today()
        return datetime.now(ZoneInfo(self.timezone)).date()

    def _set_period(self, period: Period) -> bool:
        """Show `period`; True when its dates changed (a reload is needed), False for a move inside it."""
        changed = period.key != self._period.key
        self._period = period
        self._anchor = period.start
        self.number_of_days = len(period.dates)
        return changed

    def select(self, day: date_) -> bool:
        """Select `day` (moving to its week/month if it lies outside); True if the dates shown changed."""
        if self._period.contains(day):
            return self._set_period(Period(**{**{f.name: getattr(self._period, f.name) for f in fields(Period)},
                                              "selected": day}))
        return self._set_period(Period.for_date(self.mode, day))

    def shift(self, delta: int) -> bool:
        return self._set_period(self._period.shifted(delta))

    def go_today(self) -> bool:
        return self.select(self.today())

    def go_to_month(self, year: int, month: int) -> bool:
        return self.select(date_(year, month, min(self._period.selected.day, days_in_month(year, month))))

    def month_choices(self) -> list[tuple[int, int, str]]:
        """The current year's months (year, month, label), for the month choice."""
        year = self.today().year
        return [(*shift_month(year, 1, offset), month_title(*shift_month(year, 1, offset))) for offset in range(12)]

    def move_to(self, value: str | date_) -> ControllerResult[date_]:
        """Go to a typed date (YYYY-MM-DD): its week/month is shown with the date selected."""
        if isinstance(value, str):
            try:
                value = date_.fromisoformat(value.strip())
            except ValueError:
                return ControllerResult.failure(f"The date must be YYYY-MM-DD, got {value!r}.")
        self.select(value)
        return ControllerResult.success(value)

    @property
    def form_date(self) -> date_:
        """A new task/fixed block belongs to the selected day of the week/month."""
        return self._period.selected

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load(self) -> ControllerResult[CalendarSnapshot]:
        return self.load_for(self._period)

    def load_for(self, period: Period) -> ControllerResult[CalendarSnapshot]:
        """The snapshot of `period` (safe in a worker: it reads only persisted state and `period`)."""
        try:
            return ControllerResult.success(self._calendar_snapshot(period))
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def _snapshot(self) -> CalendarSnapshot:
        return self._calendar_snapshot(self._period)

    def _calendar_snapshot(self, period: Period) -> CalendarSnapshot:
        # The task list, editing targets and executables of the period's own dates (the shared presenter).
        rows = SchedulePageController(self._planning, number_of_days=len(period.dates), anchor_date=period.start,
                                      timezone=self.timezone)
        base = self._unwrap(rows.load())

        planning = self._planning
        grid = period.grid_dates
        planning_range = self._unwrap(planning.load_range(period.grid_start, period.grid_end, scope=RangeScope.PLANNED))
        freshness = self._unwrap(planning.day_freshness(grid))
        tasks = dict(planning_range.tasks.tasks)
        placed = [p for day in grid for p in planning_range.placements_by_date[day]]
        missing = [p.task_id for p in placed if p.task_id not in tasks]
        if missing:
            tasks.update(self._unwrap(planning.get_tasks(missing)).tasks)
        shown = display_namer(planning, tasks.values(), self._unwrap)
        placed_ids = {p.task_id for p in placed}
        waiting = [task_id for task_id in planning_range.task_ids if task_id not in placed_ids]
        elsewhere = self._unwrap(planning.active_placement_dates(waiting)) if waiting else {}

        by_day: dict[date_, list[CalendarItem]] = {day: [] for day in grid}
        for day in grid:
            timed: list[tuple[int, int, str, CalendarItem]] = []
            for block in planning_range.fixed_blocks_by_date[day]:
                start = local_minutes(block.planned_start, day, block.timezone)
                end = local_minutes(block.planned_end, day, block.timezone) or MINUTES_PER_DAY
                timed.append((start, end, str(block.id), CalendarItem(
                    "fixed", block.label, block.category, RowRef("block", block.id, block.version), start, end,
                    _interval_text(block.planned_start, block.planned_end, day, block.timezone))))
            kind: ItemKind = "scheduled" if freshness[day].status == Freshness.CURRENT else "stale"
            for placement in planning_range.placements_by_date[day]:
                task = tasks.get(placement.task_id)
                start = local_minutes(placement.planned_start, day, placement.timezone)
                end = local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY
                timed.append((start, end, str(placement.id), CalendarItem(
                    kind, shown(task) if task else "(removed task)", task.category if task else "other",
                    RowRef("task", placement.task_id, task.version if task else None), start, end,
                    _interval_text(placement.planned_start, placement.planned_end, day, placement.timezone))))
            by_day[day] = [item for *_, item in sorted(timed, key=lambda entry: entry[:3])]

        undated = 0
        for task_id in waiting:  # input (creation) order, as load_range returns it
            task = tasks[task_id]
            planned = task_planned_date(task)
            if planned is None:
                undated += 0 if elsewhere.get(task_id) else 1
                continue
            if planned not in by_day:
                continue
            dates = tuple(elsewhere.get(task_id, ()))
            by_day[planned].append(CalendarItem(
                "elsewhere" if dates else "unscheduled", shown(task), task.category,
                RowRef("task", task.id, task.version),
                elsewhere=dates))

        summaries: dict[date_, DaySummary] = {}
        if self._executions is not None:  # one execution query for the whole grid (no read per date)
            executions = self._unwrap(self._executions.executions_for_placements([p.id for p in placed])) if placed else {}
            summaries = summarize_range(grid, placed, tasks, executions)
        today = self.today()
        days = [CalendarDay(date=day, in_period=period.contains(day), is_past=day < today, is_today=day == today,
                            items=by_day[day], freshness=freshness[day].status, summary=summaries.get(day))
                for day in grid]
        stale = sum(1 for cell in days if cell.in_period and cell.freshness == Freshness.STALE)
        current = sum(1 for cell in days if cell.in_period and cell.freshness == Freshness.CURRENT)
        status = (f"{current} date(s) have a current schedule, {stale} are out of date. "
                  "Open a day to make or update its schedule.")
        base_fields = {f.name: getattr(base, f.name) for f in fields(PageSnapshot)}
        base_fields["status_text"] = status
        projects = self._unwrap(planning.list_projects())
        return CalendarSnapshot(**base_fields, period=period, today=today, days=days, undated_count=undated,
                                projects={project.id: project.name for project in projects},
                                task_projects={task.id: task.project_id for task in tasks.values()})

    # ------------------------------------------------------------------
    # Reset Week / Month
    # ------------------------------------------------------------------

    def _range_label(self) -> str:
        period = self._period
        if period.mode == "month":
            return month_title(period.start.year, period.start.month)
        return f"the week of {day_label(period.start)} – {day_label(period.end)}"

    def reset_plan(self) -> ControllerResult[ResetPlan]:
        """What resetting the period's own dates would delete (nothing is written), in words."""
        period = self._period
        preview = self._planning.reset_preview(period.start, period.end)
        if not preview.ok:
            return ControllerResult.failure(preview.error, preview.cause)
        what = "this month" if period.mode == "month" else "this week"
        return ControllerResult.success(ResetPlan(
            preview.value, describe_reset(preview.value, self._range_label(), self._names, what=what)))

    def reset_period(self, plan: ResetPlan) -> ControllerResult[CalendarSnapshot]:
        """Apply exactly the previewed reset, atomically (refused, deleting nothing, if anything changed)."""
        preview = plan.preview
        result = self._planning.reset_range(preview.start_date, preview.end_date, confirmation=preview.token)
        if not result.ok:
            return self._fail_with_reload(result.error)
        return self.load()

    def _names(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        try:
            return self._task_names(list(dict.fromkeys(task_ids)))
        except _Failure:
            return {}
