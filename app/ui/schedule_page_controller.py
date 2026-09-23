"""
app/ui/schedule_page_controller.py

The Tk-free presenter behind one desktop schedule page (Day / Week / Month
in app/app.py). Every SchedulePage callback is a thin call into this class
followed by a redraw from the PageSnapshot it returns, so the callback
boundary itself is exercised headlessly by tests/ui/test_schedule_page_controller.py.

Data flow: widget callback -> SchedulePageController -> PlanningController
-> PlanningService -> PlanningRepository -> SQLite. Nothing here keeps its
own copy of tasks, fixed blocks, or placements: every successful mutation
is committed first and then the page is re-read from SQLite (load()); a
failed mutation also re-reads the committed state, so the page never shows
something that was not saved.

Dates: a page shows `number_of_days` real calendar dates starting at an
explicit, visible anchor date (legacy day index N == anchor + N - 1, the
app.planning.compat contract) in one explicit IANA timezone. The legacy
"minutes from midnight" form fields are converted with
app.planning.compat.legacy_minutes_to_utc, and a flexible task entered on a
day gets preferred_dates=[that date], exactly like the legacy CSV import
(app.planning.compat), so desktop-entered and imported tasks mean the same
thing. The page's tasks are RangeScope.PLANNED for its dates.

Identity: rows, dependency choices, edit targets, and executable
placements are all keyed by UUID (RowRef / ExecutablePlacement), never by a
row index, task name, or display label -- duplicate names work.

Preconditions (Milestone 3): a RowRef also carries the version of the
record as it was shown. Editing or deleting that row passes it as the
expected version, so a change made elsewhere since the page was drawn is
reported ("changed by someone else ... reload") instead of overwritten.
The version is not part of a RowRef's identity (equality/hash).

Fixed-block category: the form's category is saved on the block. When an
existing block's stored category is not one of the form's options (e.g. an
imported "sleep" or the legacy default "fixed"), the form shows the
fallback "other"; leaving it at "other" keeps the stored category rather
than silently replacing it.

Form validation keeps the legacy desktop form's rules (30-minute grid,
1-10 priority, required tag, no overlapping fixed blocks on a date); the
schedulers' own rules are untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import timedelta
from enum import Enum
from typing import Literal

from app.planning.application import BatchApplyResult, RangeScope, task_planned_date
from app.planning.csv_export import PlanningExportResult
from app.planning.csv_import import ImportMode
from app.planning.compat import legacy_day_to_date, legacy_minutes_to_utc
from app.planning.models import FixedBlock, LocalTimeWindow, ScheduledTask, Task
from app.planning.service import DayResultStatus
from app.planning.time import local_minutes, minutes_to_hhmm, validate_timezone
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from config.settings import TIME_SLOT_MINUTES

MINUTES_PER_DAY = 24 * 60

CATEGORY_OPTIONS = [
    "study",
    "sleep",
    "food",
    "exercise",
    "work",
    "event",
    "entertainment",
    "errand",
    "other",
]

FIXED_OPTIONS = ["False", "True"]

RowKind = Literal["task", "block"]
CanvasMode = Literal["fixed", "preview", "optimized", "stale"]


class InvalidFormError(ValueError):
    """A form value the page cannot turn into a valid task/fixed block."""


class ResetScope(str, Enum):
    #: Delete only the saved schedule (placements) dated in the page's range.
    SCHEDULE = "schedule"
    #: Also delete the range's fixed blocks and the tasks planned in it.
    PLANNING_DATA = "planning_data"


@dataclass(frozen=True)
class RowRef:
    kind: RowKind
    id: uuid.UUID
    #: The record's version when the row was drawn: the precondition for editing/deleting it.
    version: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TaskRow:
    ref: RowRef
    date: date_ | None
    day_label: str
    name: str
    type_label: str
    time_text: str


@dataclass(frozen=True)
class CanvasItem:
    day_index: int
    name: str
    category: str
    start_minute: int
    end_minute: int
    mode: CanvasMode
    score: float = 0.0


@dataclass(frozen=True)
class ExecutablePlacement:
    """A saved flexible placement the Execute tab can track, by identity."""

    task: Task
    placement: ScheduledTask
    label: str


@dataclass(frozen=True)
class UnscheduledRow:
    day_label: str
    name: str
    reason: str


@dataclass(frozen=True)
class PageSnapshot:
    start_date: date_
    end_date: date_
    timezone: str
    dates: list[date_]
    rows: list[TaskRow]
    canvas_items: list[CanvasItem]
    executables: list[ExecutablePlacement]
    fixed_count: int
    flexible_count: int
    status_text: str
    day_status: dict[date_, DayResultStatus] = field(default_factory=dict)

    @property
    def total_count(self) -> int:
        return self.fixed_count + self.flexible_count


@dataclass(frozen=True)
class FormState:
    """Values to pre-fill the form with when editing an existing row."""

    ref: RowRef
    values: dict[str, str]
    dependency_ids: list[uuid.UUID]


@dataclass(frozen=True)
class ImportRun:
    snapshot: PageSnapshot
    summary: str


@dataclass(frozen=True)
class ScheduleRun:
    snapshot: PageSnapshot
    unscheduled: list[UnscheduledRow]
    placed_count: int


def format_window(start_minute: int, end_minute: int) -> str:
    return f"{minutes_to_hhmm(start_minute)} - {minutes_to_hhmm(end_minute)}"


def day_label(day: date_) -> str:
    return f"{day:%a %b} {day.day}"


def default_anchor(mode_name: str, today: date_) -> date_:
    """The visible default start date for a page: today, this week's Monday, or this month's 1st."""
    if mode_name == "week":
        return today - timedelta(days=today.weekday())
    if mode_name == "month":
        return today.replace(day=1)
    return today


class SchedulePageController:
    def __init__(
        self,
        planning: PlanningController,
        *,
        number_of_days: int,
        anchor_date: date_,
        timezone: str,
    ) -> None:
        if number_of_days < 1:
            raise ValueError("number_of_days must be at least 1")
        validate_timezone(timezone)
        self._planning = planning
        self.number_of_days = number_of_days
        self._anchor = anchor_date
        self.timezone = timezone

    # ------------------------------------------------------------------
    # Range
    # ------------------------------------------------------------------

    @property
    def anchor_date(self) -> date_:
        return self._anchor

    @property
    def end_date(self) -> date_:
        return self._anchor + timedelta(days=self.number_of_days - 1)

    @property
    def dates(self) -> list[date_]:
        return [self._anchor + timedelta(days=offset) for offset in range(self.number_of_days)]

    def set_anchor_date(self, value: str | date_) -> ControllerResult[PageSnapshot]:
        if isinstance(value, str):
            try:
                value = date_.fromisoformat(value.strip())
            except ValueError:
                return ControllerResult.failure(f"Start date must be YYYY-MM-DD, got {value!r}.")
        self._anchor = value
        return self.load()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load(self) -> ControllerResult[PageSnapshot]:
        """Re-read the page's whole view from SQLite."""
        try:
            return ControllerResult.success(self._snapshot())
        except _Failure as failure:
            return ControllerResult.failure(failure.message)

    def form_state_for(self, ref: RowRef) -> ControllerResult[FormState]:
        """The stored values of one row, for editing in the form."""
        try:
            if ref.kind == "task":
                task = self._unwrap(self._planning.get_task(ref.id))
                if task is None:
                    raise _Failure("That task no longer exists.")
                planned = task_planned_date(task)
                window = task.preferred_time_window
                values = {
                    "name": task.name,
                    "day": str(self._day_index(planned)) if planned is not None and self._in_range(planned) else "1",
                    "category": task.category,
                    "tag": task.tags[0] if task.tags else "",
                    "fixed": "False",
                    "start_time": str(window.start_minute) if window else "0",
                    "end_time": str(window.end_minute) if window else str(MINUTES_PER_DAY),
                    "duration": str(task.estimated_duration_minutes),
                    "priority": str(task.priority),
                }
                return ControllerResult.success(FormState(ref=ref, values=values, dependency_ids=list(task.dependency_ids)))

            block = self._find_block(ref.id)
            values = {
                "name": block.label,
                "day": str(self._day_index(block.planned_date)),
                "category": block.category if block.category in CATEGORY_OPTIONS else "other",
                "tag": "fixed",
                "fixed": "True",
                "start_time": str(local_minutes(block.planned_start, block.planned_date, block.timezone)),
                "end_time": str(local_minutes(block.planned_end, block.planned_date, block.timezone)),
                "duration": "",
                "priority": "",
            }
            return ControllerResult.success(FormState(ref=ref, values=values, dependency_ids=[]))
        except _Failure as failure:
            return ControllerResult.failure(failure.message)

    def describe_tasks(self, task_ids: list[uuid.UUID]) -> ControllerResult[list[str]]:
        """Display labels ("name (date)") for dependency ids, in the given order."""
        try:
            registry = self._unwrap(self._planning.get_tasks(task_ids))
            labels = []
            for task_id in task_ids:
                task = registry.get(task_id)
                labels.append(self._task_display(task) if task is not None else f"(missing task {task_id})")
            return ControllerResult.success(labels)
        except _Failure as failure:
            return ControllerResult.failure(failure.message)

    # ------------------------------------------------------------------
    # Mutations (commit, then re-read from SQLite)
    # ------------------------------------------------------------------

    def submit_task_form(
        self,
        values: dict[str, str],
        *,
        dependency_ids: list[uuid.UUID] | None = None,
        editing: RowRef | None = None,
    ) -> ControllerResult[PageSnapshot]:
        """Create (editing=None) or update one task/fixed block from legacy form values."""
        try:
            validated = self._validate(values)
            target_date = legacy_day_to_date(validated["day"], self._anchor)

            expected_version = self._precondition(editing)
            if validated["fixed"]:
                if editing is not None and editing.kind != "block":
                    raise InvalidFormError("A flexible task cannot be turned into a fixed block; delete it and add a new one.")
                block = self._build_block(validated, target_date, editing)
                self._unwrap(self._planning.save_fixed_block(block, expected_version=expected_version))
            else:
                if editing is not None and editing.kind != "task":
                    raise InvalidFormError("A fixed block cannot be turned into a flexible task; delete it and add a new one.")
                task = self._build_task(validated, target_date, list(dependency_ids or []), editing)
                self._unwrap(self._planning.add_or_update_task(task, expected_version=expected_version))
        except (InvalidFormError, _Failure) as error:
            return self._fail_with_reload(str(error))
        return self.load()

    def delete(self, ref: RowRef) -> ControllerResult[PageSnapshot]:
        try:
            expected_version = self._precondition(ref)
            if ref.kind == "task":
                self._explain_dependents(ref.id)
                self._unwrap(self._planning.remove_task(ref.id, expected_version=expected_version))
            else:
                self._unwrap(self._planning.delete_fixed_block(ref.id, expected_version=expected_version))
        except (InvalidFormError, _Failure) as error:
            return self._fail_with_reload(str(error))
        return self.load()

    def make_schedule(self) -> ControllerResult[ScheduleRun]:
        """Allocate the page's dates, generate each date, save the range atomically, re-read."""
        run = self._planning.schedule_range(self._anchor, self.end_date, scope=RangeScope.PLANNED)
        if not run.ok:
            reloaded = self.load()
            return ControllerResult.failure(
                f"{run.error}\n\nNothing was saved; the previously saved schedule is unchanged."
                if reloaded.ok else f"{run.error}\n\n{reloaded.error}"
            )

        loaded = self.load()
        if not loaded.ok:
            return ControllerResult.failure(loaded.error)

        result = run.value
        names = self._task_names(
            [entry.task_id for entry in result.allocation.unallocated]
            + [entry.task_id for output in result.outputs.values() for entry in output.unscheduled]
        )
        unscheduled = [
            UnscheduledRow(day_label="(range)", name=names.get(entry.task_id, str(entry.task_id)),
                           reason=f"{entry.reason_code.value}: {entry.explanation}")
            for entry in result.allocation.unallocated
        ]
        for day, output in result.outputs.items():
            unscheduled.extend(
                UnscheduledRow(day_label=day_label(day), name=names.get(entry.task_id, str(entry.task_id)),
                               reason=f"{entry.reason_code.value}: {entry.explanation}")
                for entry in output.unscheduled
            )
        placed = sum(len(output.placements) for output in result.outputs.values())
        return ControllerResult.success(ScheduleRun(snapshot=loaded.value, unscheduled=unscheduled, placed_count=placed))

    def reset(self, scope: ResetScope) -> ControllerResult[PageSnapshot]:
        """Clear this page's range per `scope`. Execution history is never deleted here."""
        cleared = self._planning.clear_range(
            self._anchor, self.end_date, include_planning_data=scope == ResetScope.PLANNING_DATA
        )
        if not cleared.ok:
            return self._fail_with_reload(cleared.error)
        return self.load()

    def import_csv(self, path: str, mode: ImportMode) -> ControllerResult[ImportRun]:
        """Import a legacy CSV with day 1 == this page's start date, in this page's timezone (all or nothing)."""
        result = self._planning.import_csv_file(path, anchor_date=self._anchor, mode=mode)
        if not result.ok:
            return self._fail_with_reload(result.error)
        applied = result.value
        loaded = self.load()
        if not loaded.ok:
            return ControllerResult.failure(loaded.error)
        if isinstance(applied, BatchApplyResult):
            return ControllerResult.success(ImportRun(snapshot=loaded.value, summary=_batch_summary(applied)))
        summary = f"Imported {len(applied.tasks)} task(s) and {len(applied.fixed_blocks)} fixed block(s)"
        if applied.replaced_range is not None:
            start, end = applied.replaced_range
            cleared = applied.cleared
            summary += (
                f", replacing {start.isoformat()} to {end.isoformat()} (removed {cleared.deleted_tasks} task(s), "
                f"{cleared.deleted_fixed_blocks} fixed block(s), {cleared.deleted_placements} scheduled entr(ies))"
            )
        return ControllerResult.success(ImportRun(snapshot=loaded.value, summary=summary + "."))

    def import_description(self, mode: ImportMode) -> str:
        base = (
            f"Day 1 of the file is {self._anchor.isoformat()} (this page's start date), times are in "
            f"{self.timezone}. The whole file is checked first; if anything is invalid nothing is saved."
        )
        if mode == ImportMode.APPEND:
            return base + (
                " Append adds every row as a new task or fixed block (importing the same file twice adds it twice)."
                " A stored-planning CSV exported by this app (with record ids) is instead merged by id: records "
                "already saved unchanged are skipped, and a record that differs is refused, never overwritten."
            )
        return base + (
            " Replace first deletes the saved schedule, fixed blocks, and tasks planned on every date the "
            "file covers (first to last day), then adds the file. Execution history is kept."
        )

    def export_csv(self, path: str) -> ControllerResult[PlanningExportResult]:
        """Export this page's dates from SQLite (read-only)."""
        return self._planning.export_planning_csv(path, start_date=self._anchor, end_date=self.end_date)

    def reset_description(self, scope: ResetScope) -> str:
        span = f"{self._anchor.isoformat()} to {self.end_date.isoformat()}"
        if scope == ResetScope.SCHEDULE:
            what = f"the saved schedule (generated placements) dated {span}"
        else:
            what = f"the saved schedule, fixed blocks, and tasks planned for {span}"
        return (
            f"This permanently deletes {what}. Tasks and blocks on other dates are kept. "
            "Execution history (work sessions, feedback) is NOT deleted; use the Productivity "
            "page's separate reset for that."
        )

    # ------------------------------------------------------------------
    # Snapshot building
    # ------------------------------------------------------------------

    def _snapshot(self) -> PageSnapshot:
        start, end = self._anchor, self.end_date
        planning_range = self._unwrap(self._planning.load_range(start, end, scope=RangeScope.PLANNED))
        tasks = dict(planning_range.tasks.tasks)

        placements = [p for day in self.dates for p in planning_range.placements_by_date[day]]
        missing = [p.task_id for p in placements if p.task_id not in tasks]
        if missing:
            tasks.update(self._unwrap(self._planning.get_tasks(missing)).tasks)

        # Persisted status (see PlanningController.day_states): a date with a
        # saved schedule -- even an empty one -- is either current or stale.
        day_status: dict[date_, DayResultStatus] = {
            day: state.status
            for day, state in self._unwrap(self._planning.day_states(self.dates)).items()
            if state.status != DayResultStatus.ALLOCATED
        }

        rows: list[tuple[tuple, TaskRow]] = []
        canvas: list[CanvasItem] = []
        fixed_count = 0

        for day in self.dates:
            for block in planning_range.fixed_blocks_by_date[day]:
                fixed_count += 1
                start_minute = local_minutes(block.planned_start, day, block.timezone)
                end_minute = local_minutes(block.planned_end, day, block.timezone) or MINUTES_PER_DAY
                rows.append(((day, start_minute, 0, str(block.id)), TaskRow(
                    ref=RowRef("block", block.id, block.version), date=day, day_label=day_label(day), name=block.label,
                    type_label="fixed", time_text=format_window(start_minute, end_minute),
                )))
                canvas.append(CanvasItem(self._day_index(day), block.label, "fixed", start_minute, end_minute, "fixed"))

        placed_task_ids = {p.task_id for p in placements}
        for placement in placements:
            task = tasks[placement.task_id]
            day = placement.planned_date
            status = day_status.get(day)
            canvas.append(CanvasItem(
                self._day_index(day), task.name, task.category,
                local_minutes(placement.planned_start, day, placement.timezone),
                local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY,
                "optimized" if status == DayResultStatus.GENERATED else "stale",
                placement.score,
            ))

        for task_id in planning_range.task_ids:
            task = tasks[task_id]
            planned = task_planned_date(task)
            window = task.preferred_time_window
            window_text = f"pref {format_window(window.start_minute, window.end_minute)}" if window else "any time"
            sort_start = window.start_minute if window else 0
            rows.append((((planned or date_.max), sort_start, 1, str(task.id)), TaskRow(
                ref=RowRef("task", task.id, task.version), date=planned,
                day_label=day_label(planned) if planned is not None else "any",
                name=task.name, type_label="flexible", time_text=window_text,
            )))
            if task.id not in placed_task_ids and planned is not None and self._in_range(planned) and window:
                canvas.append(CanvasItem(
                    self._day_index(planned), f"{task.name}  · pref", task.category,
                    window.start_minute, window.end_minute, "preview",
                ))

        executables = self._executables(placements, tasks)

        return PageSnapshot(
            start_date=start,
            end_date=end,
            timezone=self.timezone,
            dates=self.dates,
            rows=[row for _, row in sorted(rows, key=lambda item: item[0])],
            canvas_items=canvas,
            executables=executables,
            fixed_count=fixed_count,
            flexible_count=len(planning_range.task_ids),
            status_text=self._status_text(day_status),
            day_status=day_status,
        )

    def _executables(self, placements: list[ScheduledTask], tasks: dict[uuid.UUID, Task]) -> list[ExecutablePlacement]:
        executables: list[ExecutablePlacement] = []
        seen: dict[str, int] = {}
        for placement in placements:
            task = tasks[placement.task_id]
            day = placement.planned_date
            window = format_window(
                local_minutes(placement.planned_start, day, placement.timezone),
                local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY,
            )
            label = f"{day_label(day)} {window}  {task.name}"
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} (#{seen[label]})"
            executables.append(ExecutablePlacement(task=task, placement=placement, label=label))
        return executables

    @staticmethod
    def _status_text(day_status: dict[date_, DayResultStatus]) -> str:
        if not day_status:
            return "No saved schedule for these dates yet. Run Make Schedule."
        if all(status == DayResultStatus.GENERATED for status in day_status.values()):
            return "Showing the saved schedule; it is current."
        return (
            "Showing the saved schedule, which is out of date (something it depends on changed since it "
            "was made, or it was saved before schedules were tracked). Run Make Schedule to refresh it."
        )

    # ------------------------------------------------------------------
    # Form -> canonical models
    # ------------------------------------------------------------------

    def _validate(self, values: dict[str, str]) -> dict[str, object]:
        name = values.get("name", "").strip()
        category = values.get("category", "").strip()
        tag = values.get("tag", "").strip()
        fixed_text = values.get("fixed", "False").strip()

        if not name:
            raise InvalidFormError("Name is required.")
        day = _parse_int(values.get("day", "1"), "Day")
        if not 1 <= day <= self.number_of_days:
            raise InvalidFormError(f"Day must be between 1 and {self.number_of_days}.")
        if category not in CATEGORY_OPTIONS:
            raise InvalidFormError("Category must be selected from the category dropdown.")
        if not tag:
            raise InvalidFormError("Tag is required.")
        if fixed_text not in FIXED_OPTIONS:
            raise InvalidFormError("Fixed must be either True or False.")

        fixed = fixed_text == "True"
        start_time = _parse_int(values.get("start_time", ""), "Start time")
        end_time = _parse_int(values.get("end_time", ""), "End time")
        if start_time < 0 or end_time > MINUTES_PER_DAY:
            raise InvalidFormError("Times must be between 0 and 1440 minutes.")
        if start_time >= end_time:
            raise InvalidFormError("Start time must be smaller than end time.")
        if start_time % TIME_SLOT_MINUTES or end_time % TIME_SLOT_MINUTES:
            raise InvalidFormError(f"Start and end times must be multiples of {TIME_SLOT_MINUTES} minutes.")

        duration, priority = 0, 1
        if not fixed:
            duration = _parse_int(values.get("duration", ""), "Duration")
            priority = _parse_int(values.get("priority", ""), "Priority")
            if duration <= 0:
                raise InvalidFormError("Duration must be greater than 0.")
            if duration % TIME_SLOT_MINUTES:
                raise InvalidFormError(f"Duration must be a multiple of {TIME_SLOT_MINUTES} minutes.")
            if duration > MINUTES_PER_DAY:
                raise InvalidFormError("Duration cannot be longer than 24 hours.")
            if not 1 <= priority <= 10:
                raise InvalidFormError("Priority must be between 1 and 10.")

        return {
            "name": name, "day": day, "category": category, "tag": tag, "fixed": fixed,
            "start_time": start_time, "end_time": end_time, "duration": duration, "priority": priority,
        }

    def _build_block(self, validated: dict, target_date: date_, editing: RowRef | None) -> FixedBlock:
        start_utc, end_utc = legacy_minutes_to_utc(target_date, self.timezone, validated["start_time"], validated["end_time"])
        block_id = editing.id if editing is not None else uuid.uuid4()
        category = validated["category"]
        stored = None
        if editing is not None:
            stored = self._find_block(editing.id)  # must still exist
            if category == "other" and stored.category not in CATEGORY_OPTIONS:
                category = stored.category

        others = self._unwrap(self._planning.get_fixed_blocks(target_date))
        for other in others:
            if other.id != block_id and start_utc < other.planned_end and other.planned_start < end_utc:
                raise InvalidFormError(f"Fixed task overlaps with existing fixed task: {other.label}")

        fields = dict(
            id=block_id, label=validated["name"], category=category, planned_date=target_date, timezone=self.timezone,
            planned_start=start_utc, planned_end=end_utc,
        )
        if stored is not None:
            fields.update(user_id=stored.user_id, created_at=stored.created_at, version=stored.version)
        return FixedBlock(**fields)

    def _build_task(
        self, validated: dict, target_date: date_, dependency_ids: list[uuid.UUID], editing: RowRef | None
    ) -> Task:
        fields = {
            "name": validated["name"],
            "category": validated["category"],
            "estimated_duration_minutes": validated["duration"],
            "priority": validated["priority"],
            "preferred_time_window": LocalTimeWindow(
                start_minute=validated["start_time"], end_minute=validated["end_time"]
            ),
            "dependency_ids": dependency_ids,
        }
        try:
            if editing is None:
                return Task(**fields, tags=[validated["tag"]], preferred_dates=[target_date])

            stored = self._unwrap(self._planning.get_task(editing.id))
            if stored is None:
                raise _Failure("That task no longer exists.")
            data = stored.model_dump()
            data.update(fields)
            data["tags"] = [validated["tag"], *stored.tags[1:]]
            if task_planned_date(stored) != target_date:
                if stored.required_date is not None:
                    data["required_date"] = target_date
                else:
                    data["preferred_dates"] = [target_date]
            return Task.model_validate(data)
        except ValueError as error:  # pydantic ValidationError, e.g. a self-dependency
            raise InvalidFormError(_first_validation_message(error)) from error

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _precondition(ref: RowRef | None) -> int | None:
        """The expected version for editing/deleting `ref` (None: creating a new record)."""
        if ref is None:
            return None
        if ref.version is None:
            raise InvalidFormError("This row has no saved version to compare against; reload the page and try again.")
        return ref.version

    def _explain_dependents(self, task_id: uuid.UUID) -> None:
        """Turn "other tasks depend on this" into a readable, name-based message before deleting."""
        tasks = self._unwrap(self._planning.list_tasks())
        dependents = [task for task in tasks if task_id in task.dependency_ids]
        if dependents:
            names = ", ".join(self._task_display(task) for task in dependents)
            raise _Failure(
                f"Cannot delete this task: {names} depend(s) on it. Remove that dependency first, "
                "or delete the dependent task(s)."
            )

    def _task_names(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not task_ids:
            return {}
        registry = self._unwrap(self._planning.get_tasks(task_ids))
        return {task_id: task.name for task_id, task in registry.tasks.items()}

    def _task_display(self, task: Task) -> str:
        planned = task_planned_date(task)
        return f"{task.name} ({day_label(planned)})" if planned is not None else task.name

    def _find_block(self, block_id: uuid.UUID) -> FixedBlock:
        for day in self.dates:
            for block in self._unwrap(self._planning.get_fixed_blocks(day)):
                if block.id == block_id:
                    return block
        raise _Failure("That fixed block no longer exists on these dates.")

    def _in_range(self, day: date_) -> bool:
        return self._anchor <= day <= self.end_date

    def _day_index(self, day: date_) -> int:
        return (day - self._anchor).days + 1

    def _fail_with_reload(self, message: str | None) -> ControllerResult:
        """A failed mutation: re-read the committed state so the caller can redraw it, and report."""
        reloaded = self.load()
        text = message or "An unknown error occurred."
        if not reloaded.ok:
            text = f"{text}\n\nAdditionally, reloading saved data failed: {reloaded.error}"
        return ControllerResult(ok=False, value=reloaded.value, error=text)

    @staticmethod
    def _unwrap(result: ControllerResult):
        if not result.ok:
            raise _Failure(result.error or "An unknown error occurred.")
        return result.value


def _batch_summary(applied: BatchApplyResult) -> str:
    def counts(kind_counts: dict[str, int]) -> str:
        return ", ".join(f"{count} {kind.replace('_', ' ')}(s)" for kind, count in kind_counts.items() if count) or "nothing"

    return (
        f"Merged the stored-planning CSV by id: created {counts(applied.created)}; updated {counts(applied.updated)}; "
        f"deleted {counts(applied.deleted)}; already up to date: {counts(applied.unchanged)}."
    )


class _Failure(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_int(value: str, field_name: str) -> int:
    value = (value or "").strip()
    if value == "":
        raise InvalidFormError(f"{field_name} is required.")
    try:
        return int(value)
    except ValueError as error:
        raise InvalidFormError(f"{field_name} must be an integer.") from error


def _first_validation_message(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        details = errors()
        if details:
            return str(details[0].get("msg", error))
    return str(error)
