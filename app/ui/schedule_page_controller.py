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

Recurring series (docs/recurrence.md): a series definition is listed as its
own row ("repeats", or "needs setup" when it was saved before series
repeated) whenever it can produce a date of the page, and its materialized
occurrences are the page's ordinary rows ("repeat"). Saving an occurrence
or deleting it takes an EditScope -- this occurrence, this and every later
one (from its original date), or the entire series (scope_choices /
removal_choices say which apply to a row); a series row's own edits apply
to the entire series. Make Schedule materializes the range's occurrences
first (PlanningController.schedule_range).

The desktop form (Milestone 4) saves through blank_draft/editor_options/
draft_for/save_draft/delete_description with app/ui/task_form_model.py's
minute-precise rules (docs/desktop-task-form.md). submit_task_form and
form_state_for remain for existing headless callers and keep the legacy
form's rules (30-minute grid, 1-10 priority, required tag); the schedulers'
own rules are untouched either way.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date as date_
from datetime import timedelta
from enum import Enum
from typing import Literal

from app.execution.models import TaskExecution
from app.planning.application import BatchApplyResult, RangeScope, task_planned_date
from app.planning.csv_export import PlanningExportResult
from app.planning.csv_import import ImportMode
from app.planning.compat import legacy_day_to_date, legacy_minutes_to_utc
from app.planning.models import FixedBlock, LocalTimeWindow, ScheduledTask, Task, task_display_name
from app.planning.series import EditScope
from app.planning.fixed_block_rules import FixedBlockRuleViolation
from app.planning.service import DayResultStatus
from app.planning.time import local_date_of, local_day_start_utc, local_minutes, validate_timezone
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from app.ui.task_form_model import (
    Choice,
    EditorOptions,
    FormErrors,
    TaskDraft,
    build_block,
    build_task,
    build_todo,
    categories_for,
    describe_rule,
    draft_from_block,
    draft_from_task,
)
from app.ui.time_fields import format_clock
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
    """A saved flexible placement whose execution the Day page's status board tracks, by identity."""

    task: Task
    placement: ScheduledTask
    label: str
    #: The task's name as schedule views show it (with its project's abbreviation); "" = the task's own name.
    display_name: str = ""


@dataclass(frozen=True)
class DirectCompletion:
    """A task completed without a time slot (from its project), shown on the date it was completed."""

    execution: TaskExecution
    #: The task as it is now (a removed task's completion is not listed).
    task: Task
    display_name: str


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
    """0..1440 minutes as readable local times, e.g. "9:00 AM – 10:30 AM"."""
    return f"{format_clock(start_minute)} – {format_clock(end_minute)}"


def day_label(day: date_) -> str:
    return f"{day:%a %b} {day.day}"


def display_namer(planning: PlanningController, tasks, unwrap) -> Callable[[Task], str]:
    """
    task -> its name as schedule views show it: "name (abc)" for a task of a
    live project (models.task_display_name), else its own name. Built from the
    projects as they are named now, so a rename shows at the next redraw; the
    stored task name is never changed. `unwrap` turns a failed read into the
    caller's own failure.
    """
    names: dict[uuid.UUID, str] = {}
    if any(task.project_id is not None for task in tasks):
        names = {project.id: project.name for project in unwrap(planning.list_projects())}
    return lambda task: task_display_name(task.name, names.get(task.project_id))


def read_direct_completions(planning: PlanningController, day: date_, timezone_name: str, unwrap
                            ) -> list[DirectCompletion]:
    """
    The work completed on the local date `day` without a placement, oldest
    first: one windowed read of the completion history (the record the
    analytics use), never a scan of every execution. Only tasks that still
    exist are listed: a removed task leaves the Day page like a removed
    scheduled one does. To Dos are not listed here: a To Do is shown on the
    day it belongs to, whenever it was ticked (todo_completions).
    """
    start = local_day_start_utc(day, timezone_name)
    history = unwrap(planning.completion_history(start, local_day_start_utc(day + timedelta(days=1), timezone_name)))
    found = [execution for execution in history.executions
             if execution.scheduled_task_id is None and execution.task_id is not None
             and local_date_of(execution.actual_final_end_at, timezone_name) == day]
    tasks = {task_id: task for task_id, task in history.tasks.items()
             if task.deleted_at is None and not task.is_todo}
    found = [execution for execution in found if execution.task_id in tasks]
    shown = display_namer(planning, tasks.values(), unwrap)
    return [DirectCompletion(execution, tasks[execution.task_id], shown(tasks[execution.task_id]))
            for execution in sorted(found, key=lambda item: (item.actual_final_end_at, item.id))]


def todo_completions(todos, executions: dict[uuid.UUID, TaskExecution]) -> list[DirectCompletion]:
    """
    The completed ones of a day's To Dos (`executions`: their completion
    records by task id), as the records its Completed list shows: on the
    day the To Do belongs to, not on the date it happened to be ticked.
    """
    done = [(task, executions[task.id]) for task in todos
            if task.id in executions and executions[task.id].status.value == "completed"]
    return [DirectCompletion(execution, task, task.name)
            for task, execution in sorted(done, key=lambda pair: (pair[1].actual_final_end_at, pair[1].id))]


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
    def planning(self) -> PlanningController:
        """The planning controller this page works through."""
        return self._planning

    @property
    def anchor_date(self) -> date_:
        return self._anchor

    @property
    def end_date(self) -> date_:
        return self._anchor + timedelta(days=self.number_of_days - 1)

    @property
    def dates(self) -> list[date_]:
        return [self._anchor + timedelta(days=offset) for offset in range(self.number_of_days)]

    def detached(self):
        """
        A copy frozen at the dates shown now, for one storage call in a worker:
        whatever the page navigates to meanwhile, the call reads and re-reads
        the dates it was started for, and it cannot change this controller's
        own dates. The copy shares the (thread-safe) planning controller.
        """
        return copy.copy(self)

    def move_to(self, value: str | date_) -> ControllerResult[date_]:
        """Show another date (typed as YYYY-MM-DD, or a date); nothing is read from storage."""
        if isinstance(value, str):
            try:
                value = date_.fromisoformat(value.strip())
            except ValueError:
                return ControllerResult.failure(f"Start date must be YYYY-MM-DD, got {value!r}.")
        self._anchor = value
        return ControllerResult.success(value)

    def set_anchor_date(self, value: str | date_) -> ControllerResult[PageSnapshot]:
        moved = self.move_to(value)
        if not moved.ok:
            return ControllerResult.failure(moved.error)
        return self.load()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load(self) -> ControllerResult[PageSnapshot]:
        """Re-read the page's whole view from SQLite."""
        try:
            return ControllerResult.success(self._snapshot())
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

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
            return ControllerResult.failure(failure.message, failure.cause)

    # ------------------------------------------------------------------
    # The reusable task form (app/ui/task_form_model.py)
    # ------------------------------------------------------------------

    @property
    def form_date(self) -> date_:
        """The real date a task or fixed block added on this page gets (the Day page's date)."""
        return self._anchor

    def blank_draft(self, kind: str = "task") -> TaskDraft:
        """An empty form for this page, dated on form_date, with no category chosen yet."""
        return TaskDraft(kind=kind, category="", date=self.form_date.isoformat())

    def editor_options(self, editing: RowRef | None = None, *, category: str | None = None,
                       project_id: uuid.UUID | None = None) -> ControllerResult[EditorOptions]:
        """
        The form's choices in this workspace: categories (a stored unknown
        one kept), tasks that can be dependencies (never the edited task;
        labels include the date, and a short id where names still collide)
        and projects -- all kept by id.
        """
        try:
            tasks = [task for task in self._unwrap(self._planning.list_tasks())
                     if not task.is_todo
                     and not (editing is not None and editing.kind == "task" and task.id == editing.id)]
            labels = {task.id: self._task_display(task) for task in tasks}
            counts: dict[str, int] = {}
            for label in labels.values():
                counts[label] = counts.get(label, 0) + 1
            dependencies = sorted(
                (Choice(task.id, labels[task.id] if counts[labels[task.id]] == 1 else f"{labels[task.id]} #{str(task.id)[:4]}")
                 for task in tasks),
                key=lambda choice: choice.label.lower(),
            )
            live = self._unwrap(self._planning.list_projects())
            names: dict[str, int] = {}
            for project in live:
                names[project.name] = names.get(project.name, 0) + 1
            projects = sorted(
                (Choice(project.id, project.name if names[project.name] == 1 else f"{project.name} #{str(project.id)[:4]}")
                 for project in live),
                key=lambda choice: choice.label.lower())
            if project_id is not None and project_id not in {project.id for project in live}:
                # A reference to a deleted/unknown project is shown as it is, never silently cleared on save.
                deleted = {p.id: p for p in self._unwrap(self._planning.list_projects(include_deleted=True))}
                gone = deleted.get(project_id)
                label = (f"{gone.name} (deleted project)" if gone is not None
                         else f"Unknown project {str(project_id)[:8]}")
                projects.insert(0, Choice(project_id, label))
            types = self._unwrap(self._planning.list_task_types())
            type_names: dict[str, int] = {}
            for task_type in types:
                type_names[task_type.label] = type_names.get(task_type.label, 0) + 1
            task_types = sorted(
                (Choice(task_type.id, task_type.label if type_names[task_type.label] == 1
                        else f"{task_type.label} #{str(task_type.id)[:4]}") for task_type in types),
                key=lambda choice: choice.label.lower())
            return ControllerResult.success(EditorOptions(
                timezone=self.timezone, categories=categories_for(category), dependencies=dependencies,
                projects=projects, task_types=task_types,
            ))
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def draft_for(self, ref: RowRef) -> ControllerResult[TaskDraft]:
        """The stored values of one row as a form draft (for editing)."""
        try:
            if ref.kind == "task":
                task = self._unwrap(self._planning.get_task(ref.id))
                if task is None:
                    raise _Failure("That task no longer exists.")
                series = (self._unwrap(self._planning.get_task_including_deleted(task.series_id))
                          if task.is_occurrence else None)
                return ControllerResult.success(draft_from_task(task, self.timezone, series=series))
            return ControllerResult.success(draft_from_block(self._find_block(ref.id)))
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def scope_choices(self, ref: RowRef) -> ControllerResult[list[tuple[str, str]]]:
        """(EditScope value, label) choices for saving an edit of `ref`: several only for an occurrence."""
        try:
            task = self._task_or_none(ref)
            if task is not None and task.is_occurrence:
                return ControllerResult.success([
                    (EditScope.OCCURRENCE.value, "Only this occurrence"),
                    (EditScope.FUTURE.value, "This and every later occurrence"),
                    (EditScope.SERIES.value, "Every occurrence (the entire series)"),
                ])
            if task is not None and task.is_series:
                return ControllerResult.success([(EditScope.SERIES.value, "The entire series")])
            return ControllerResult.success([])
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def removal_choices(self, ref: RowRef) -> ControllerResult[list[tuple[str, str]]]:
        """(choice, label) for removing `ref`: "skip", "occurrence", "future", "series"; empty for other rows."""
        try:
            task = self._task_or_none(ref)
            if task is not None and task.is_occurrence:
                return ControllerResult.success([
                    ("skip", "Skip only this occurrence"),
                    (EditScope.OCCURRENCE.value, "Delete only this occurrence"),
                    (EditScope.FUTURE.value, "Delete this and every later occurrence"),
                    (EditScope.SERIES.value, "Delete every occurrence (the entire series)"),
                ])
            if task is not None and task.is_series:
                return ControllerResult.success([(EditScope.SERIES.value, "Delete the entire series")])
            return ControllerResult.success([])
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def _task_or_none(self, ref: RowRef) -> Task | None:
        return self._unwrap(self._planning.get_task(ref.id)) if ref.kind == "task" else None

    def save_draft(self, draft: TaskDraft, *, editing: RowRef | None = None,
                   scope: str | None = None) -> ControllerResult[PageSnapshot]:
        """
        Create (editing=None) or update one task/fixed block from the form.
        Edits start from the stored record and pass the version the row was
        drawn with, so a change made elsewhere meanwhile is reported, not
        overwritten. On any failure nothing is saved; the error's cause is
        FormErrors (field -> message) where a field is to blame, and the
        committed state is re-read. Editing an occurrence of a series applies
        to `scope` (an EditScope value; default: only this occurrence); a
        series row's edit applies to the entire series.
        """
        try:
            expected_version = self._precondition(editing)
            if editing is not None and editing.kind != ("block" if draft.kind == "block" else "task"):
                raise InvalidFormError("A task cannot become a fixed block (or the other way round) by editing; "
                                       "remove it and add the other kind.")
            if draft.kind == "todo":
                stored = None
                if editing is not None:
                    stored = self._unwrap(self._planning.get_task(editing.id))
                    if stored is None:
                        raise _Failure("That To Do no longer exists.")
                    if not stored.is_todo:
                        raise InvalidFormError("A scheduled task cannot become a To Do by editing; remove it and "
                                               "add a To Do.")
                result = self._planning.add_or_update_task(build_todo(draft, existing=stored),
                                                           expected_version=expected_version)
                if not result.ok:
                    raise _FormFailure(result.error or "The To Do could not be saved.", result.cause)
            elif draft.kind == "block":
                stored = self._find_block(editing.id) if editing is not None else None
                block = build_block(draft, timezone_name=self.timezone, existing=stored)
                result = self._planning.save_fixed_block(block, expected_version=expected_version)
                if not result.ok:
                    raise _FormFailure(self._block_refusal(result), result.cause)
            else:
                stored = None
                if editing is not None:
                    stored = self._unwrap(self._planning.get_task(editing.id))
                    if stored is None:
                        raise _Failure("That task no longer exists.")
                if stored is not None and stored.is_todo:
                    raise InvalidFormError("A To Do cannot become a scheduled task by editing; remove it and add "
                                           "a task.")
                if stored is not None and stored.is_series and draft.needs_configuration and not draft.date.strip():
                    draft = replace(draft, date=self.form_date.isoformat())  # configuring: it starts on this page's date
                if draft.new_type_label.strip():
                    created = self._planning.create_task_type(draft.new_type_label)
                    if not created.ok:
                        raise _FormFailure(created.error or "The task type could not be created.", created.cause)
                    draft = replace(draft, task_type_id=created.value.id, new_type_label="")
                task = build_task(draft, timezone_name=self.timezone, existing=stored)
                if stored is not None and (stored.is_occurrence or stored.is_series):
                    result = self._save_series_edit(stored, task, expected_version, scope)
                else:
                    result = self._planning.add_or_update_task(task, expected_version=expected_version)
                if not result.ok:
                    raise _FormFailure(result.error or "The task could not be saved.", result.cause)
        except FormErrors as errors:
            failed = self._fail_with_reload("; ".join(errors.errors.values()))
            return ControllerResult(ok=False, value=failed.value, error=failed.error, cause=errors)
        except _FormFailure as failure:
            failed = self._fail_with_reload(failure.message)
            return ControllerResult(ok=False, value=failed.value, error=failed.error, cause=failure.cause)
        except (InvalidFormError, _Failure) as error:
            return self._fail_with_reload(str(error))
        return self.load()

    def _save_series_edit(self, stored: Task, edited: Task, expected_version: int, scope: str | None):
        """An edit of a series definition or of one of its occurrences, for the chosen scope."""
        if stored.is_series:
            return self._planning.edit_series(edited, expected_version=expected_version, scope=EditScope.SERIES)
        chosen = EditScope(scope or EditScope.OCCURRENCE.value)
        if chosen == EditScope.OCCURRENCE:
            return self._planning.edit_occurrence(edited, expected_version=expected_version)
        series = self._unwrap(self._planning.get_task(stored.series_id))
        if series is None:
            raise _Failure("This occurrence's series was deleted; only this occurrence can be changed.")
        content = {name: getattr(edited, name) for name in (
            "project_id", "name", "category", "tags", "estimated_duration_minutes", "priority", "points", "required",
            "preferred_time", "preferred_time_window")}
        definition = series.model_copy(update=content)
        return self._planning.edit_series(definition, expected_version=series.version, scope=chosen,
                                          cutoff=stored.occurrence_slot if chosen == EditScope.FUTURE else None)

    def delete_description(self, ref: RowRef) -> ControllerResult[str]:
        """What removing a row does, in words, for the confirmation."""
        try:
            if ref.kind == "task":
                task = self._unwrap(self._planning.get_task(ref.id))
                if task is None:
                    raise _Failure("That task no longer exists.")
                if task.is_occurrence or task.is_series:
                    return ControllerResult.success(
                        f"Remove \u201c{self._task_display(task)}\u201d, a repeating task? Removed occurrences are "
                        "never generated again, and their saved schedule entries, completions and points go with "
                        "them. Occurrences already started or finished, and ones you edited on their own, are kept."
                    )
                if task.is_todo:
                    return ControllerResult.success(
                        f"Remove the To Do \u201c{self._task_display(task)}\u201d? Its completion and points are "
                        "removed with it.")
                return ControllerResult.success(
                    f"Remove the task \u201c{self._task_display(task)}\u201d? Its saved schedule entries, completions "
                    "and points are removed with it."
                )
            block = self._find_block(ref.id)
            return ControllerResult.success(
                f"Remove the fixed block \u201c{block.label}\u201d on {day_label(block.planned_date)}? Its "
                "completion and points are removed with it.")
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)

    def _block_refusal(self, result: ControllerResult) -> str:
        """A fixed-block refusal in local times, naming the block it collides with."""
        cause = result.cause
        other = getattr(cause, "conflicting", None)
        if isinstance(cause, FixedBlockRuleViolation) and cause.code == "overlap" and other is not None:
            start = local_minutes(other.planned_start, other.planned_date, other.timezone)
            end = local_minutes(other.planned_end, other.planned_date, other.timezone)
            return (f"This time overlaps the fixed block \u201c{other.label}\u201d ({format_clock(start)} \u2013 "
                    f"{format_clock(end)} on {day_label(other.planned_date)}). Choose another time.")
        return result.error or "The fixed block could not be saved."

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
            return ControllerResult.failure(failure.message, failure.cause)

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

    def delete(self, ref: RowRef, *, scope: str | None = None) -> ControllerResult[PageSnapshot]:
        """
        Remove a row. For an occurrence `scope` is "skip", "occurrence",
        "future" or "series" (default "occurrence"); for a series, "series".
        """
        try:
            expected_version = self._precondition(ref)
            task = self._task_or_none(ref)
            if task is not None and (task.is_occurrence or task.is_series):
                self._unwrap(self._delete_in_series(task, expected_version, scope))
            elif ref.kind == "task":
                self._explain_dependents(ref.id)
                self._unwrap(self._planning.remove_task(ref.id, expected_version=expected_version))
            else:
                self._unwrap(self._planning.delete_fixed_block(ref.id, expected_version=expected_version))
        except (InvalidFormError, _Failure) as error:
            return self._fail_with_reload(str(error))
        return self.load()

    def _delete_in_series(self, task: Task, expected_version: int, scope: str | None):
        if task.is_series:
            return self._planning.delete_series(task.id, expected_version=expected_version, scope=EditScope.SERIES)
        choice = scope or EditScope.OCCURRENCE.value
        if choice in ("skip", EditScope.OCCURRENCE.value):
            return self._planning.delete_occurrence(task.id, expected_version=expected_version, skip=choice == "skip")
        series = self._unwrap(self._planning.get_task(task.series_id))
        if series is None:
            raise _Failure("This occurrence's series was deleted; only this occurrence can be removed.")
        chosen = EditScope(choice)
        return self._planning.delete_series(series.id, expected_version=series.version, scope=chosen,
                                            cutoff=task.occurrence_slot if chosen == EditScope.FUTURE else None)

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
        # Work this run left where it is: occurrences already planned outside the range (never moved silently).
        elsewhere = self._task_names(list(result.kept_elsewhere))
        unscheduled.extend(
            UnscheduledRow(day_label=day_label(placement.planned_date), name=elsewhere.get(task_id, str(task_id)),
                           reason="already planned outside this range: left there (move it to plan it here)")
            for task_id, placement in sorted(result.kept_elsewhere.items(), key=lambda item: item[1].planned_date)
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
                # The block's own category (an unknown imported one included) picks its color; mode says it is fixed.
                canvas.append(CanvasItem(self._day_index(day), block.label, block.category, start_minute, end_minute,
                                         "fixed"))

        series_rows = self._series_rows(start, end)
        shown = display_namer(self._planning, [*tasks.values(), *series_rows], self._unwrap)
        for placement in placements:
            task = tasks[placement.task_id]
            day = placement.planned_date
            status = day_status.get(day)
            canvas.append(CanvasItem(
                self._day_index(day), shown(task), task.category,
                local_minutes(placement.planned_start, day, placement.timezone),
                local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY,
                "optimized" if status == DayResultStatus.GENERATED else "stale",
                placement.score,
            ))

        for task_id in planning_range.task_ids:
            task = tasks[task_id]
            planned = task_planned_date(task)
            window = task.preferred_time_window if task.preferred_time is None else None  # legacy data only
            window_text = (f"prefers {task.preferred_time.value}" if task.preferred_time is not None
                           else f"pref {format_window(window.start_minute, window.end_minute)}" if window
                           else "any time")
            sort_start = window.start_minute if window else 0
            type_label = "flexible"
            if task.is_occurrence:
                type_label = "repeat (edited)" if task.occurrence_state is not None else "repeat"
            rows.append((((planned or date_.max), sort_start, 1, str(task.id)), TaskRow(
                ref=RowRef("task", task.id, task.version), date=planned,
                day_label=day_label(planned) if planned is not None else "any",
                name=shown(task), type_label=type_label, time_text=window_text,
            )))
        for series in series_rows:
            rule = series.recurrence
            first = rule.start_date if rule.configured else None
            rows.append((((first or date_.max), 0, 2, str(series.id)), TaskRow(
                ref=RowRef("task", series.id, series.version), date=first,
                day_label=day_label(first) if first is not None else "any",
                name=shown(series), type_label="repeats" if rule.configured else "needs setup",
                time_text=describe_rule(rule) if rule.configured else "choose how it repeats",
            )))

        executables = self._executables(placements, tasks, shown)

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

    def _series_rows(self, start: date_, end: date_) -> list[Task]:
        """The series definitions to list on these dates: ones that can repeat in them, and ones needing setup."""
        found = []
        for series in self._unwrap(self._planning.list_series()):
            rule = series.recurrence
            if not rule.configured:
                found.append(series)
            elif rule.start_date <= end and (rule.end_date is None or rule.end_date >= start):
                found.append(series)
        return found

    def _executables(self, placements: list[ScheduledTask], tasks: dict[uuid.UUID, Task],
                     shown: Callable[[Task], str] = lambda task: task.name) -> list[ExecutablePlacement]:
        executables: list[ExecutablePlacement] = []
        seen: dict[str, int] = {}
        for placement in placements:
            task = tasks[placement.task_id]
            day = placement.planned_date
            window = format_window(
                local_minutes(placement.planned_start, day, placement.timezone),
                local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY,
            )
            label = f"{day_label(day)} {window}  {shown(task)}"
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} (#{seen[label]})"
            executables.append(ExecutablePlacement(task=task, placement=placement, label=label,
                                                   display_name=shown(task)))
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
            raise _Failure(result.error or "An unknown error occurred.", result.cause)
        return result.value


def _batch_summary(applied: BatchApplyResult) -> str:
    def counts(kind_counts: dict[str, int]) -> str:
        return ", ".join(f"{count} {kind.replace('_', ' ')}(s)" for kind, count in kind_counts.items() if count) or "nothing"

    return (
        f"Merged the stored-planning CSV by id: created {counts(applied.created)}; updated {counts(applied.updated)}; "
        f"deleted {counts(applied.deleted)}; already up to date: {counts(applied.unchanged)}."
    )


class _Failure(Exception):
    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: The structured error behind the message, kept so a page can tell, e.g., "signed out" from a real failure.
        self.cause = cause


class _FormFailure(_Failure):
    """A save the service refused; `cause` is its structured error (e.g. the overlapping block)."""

    def __init__(self, message: str, cause: BaseException | None) -> None:
        super().__init__(message)
        self.cause = cause


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
