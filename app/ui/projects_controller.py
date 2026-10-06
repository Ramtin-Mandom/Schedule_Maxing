"""
app/ui/projects_controller.py

The Tk-free presenter of the desktop Project Schedule (Milestone 4,
Prompt 6). Everything goes through PlanningController and the existing
PlanningService project operations: create, rename or re-describe (with the
version read as the precondition), list and delete.

- **Deleting:** only an empty project can be deleted. One that live tasks
  still belong to is refused with their names. Nothing is cascaded; the user
  can deliberately move those tasks to another project, or to none
  (reassign_tasks, one atomic, version-checked write), and then delete it.
- **Archive:** there is none, so none is offered. A project can be marked
  complete and reopened (Project.completed_at); nothing else changes with it.
- **Tasks of a project:** added with the shared task form's own validation
  and save path (SchedulePageController.save_draft) on a chosen date, without
  a time slot; an existing one is moved to a date the same way and loses its
  saved placements (PlanningController.move_task_unscheduled). A task is
  complete when one of its executions is. Removing one is the schedule
  pages' own removal (SchedulePageController.delete), with its confirmation.
- **Completing a project's task** needs no time slot -- the one exception to
  "a task is completed on its scheduled slot", and only for a task that
  belongs to a project. A scheduled one is completed on its slot (as on the
  Day page); an unscheduled one directly (app/execution/direct_completion.py),
  which the Day page then lists as completed on that date. Either way it is
  one completion: repeating the action adds nothing, and undoing withdraws it.
- **Task defaults:** a project may configure the duration, priority and
  points of its new tasks (Project.task_defaults; each unset by default).
  Precedence for a new task: what was typed, then the project's configured
  values, then the category's defaults, then the application's
  (app/ui/task_defaults.resolve_task_default).
- **Milestones:** stored with their project (Project.milestones), shown by
  number; a score is 1-10 (milestone_score_band names its color band).
- **The project's schedule:** its tasks, by id, with their planned dates and
  every saved placement (date and exact times), or "not scheduled".
  Recurring tasks keep their rule and are shown once; they are not expanded.
- **Owner scope:** everything follows the controller's workspace. New
  projects in an account workspace belong to that account.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date as date_
from datetime import datetime, timezone
from typing import Literal

from pydantic import ValidationError

from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus
from app.planning.application import task_planned_date
from app.planning.errors import EntityInUseError
from app.planning.models import (
    MAX_MILESTONE_SCORE,
    MIN_MILESTONE_SCORE,
    Project,
    ProjectMilestone,
    ProjectTaskDefaults,
    ScheduledTask,
    Task,
)
from app.planning.time import local_minutes
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import PageSnapshot, RowRef, SchedulePageController, day_label
from app.ui.task_defaults import parse_project_defaults
from app.ui.task_form_model import EditorOptions, FormErrors, TaskDraft, build_task, draft_from_task
from app.ui.time_fields import FieldError, format_clock, format_duration, parse_date

ScoreBand = Literal["neutral", "light", "green", "dark"]


def milestone_score_band(score: int) -> ScoreBand:
    """The color band of a milestone score: 1-3 neutral, 4-7 light green, 8-9 green, 10 dark green."""
    if score <= 3:
        return "neutral"
    if score <= 7:
        return "light"
    return "green" if score <= 9 else "dark"


def project_choices(projects: dict[uuid.UUID, str]) -> dict[str, uuid.UUID]:
    """Readable choices; disambiguate duplicate names without exposing long internal IDs."""
    counts = Counter(projects.values())
    choices = {}
    for key, name in projects.items():
        unique = counts[name] == 1 and name not in {"New project", "No project", "All projects"}
        label = name if unique else f"{name} ({key.hex[:8]})"
        while label in choices:  # even imported adversarial/colliding labels remain selectable by ID
            label += f" ({key})"
        choices[label] = key
    return choices


@dataclass(frozen=True)
class ProjectRow:
    id: uuid.UUID
    name: str
    description: str
    version: int
    task_count: int
    scheduled_count: int
    start_date: date_ | None = None
    estimated_end_date: date_ | None = None
    completed: bool = False
    #: What the project fills in for its new tasks (each value None unless explicitly configured).
    task_defaults: ProjectTaskDefaults = field(default_factory=ProjectTaskDefaults)

    @property
    def label(self) -> str:
        return (f"{self.name} — {self.task_count} task(s), {self.scheduled_count} scheduled"
                if self.task_count else f"{self.name} — no tasks")

    @property
    def dates_text(self) -> str:
        """"Sep 1 – Oct 15" style span; "" when neither date is set."""
        if self.start_date is None and self.estimated_end_date is None:
            return ""
        start = day_label(self.start_date) if self.start_date else "no start date"
        end = day_label(self.estimated_end_date) if self.estimated_end_date else "no end date"
        return f"{start} – {end}"


@dataclass(frozen=True)
class ProjectTaskRow:
    task_id: uuid.UUID
    name: str
    date_text: str
    duration_text: str
    status: str
    #: Dates the task is scheduled on (each can be opened on the Day page); else its planned date, if any.
    dates: tuple[date_, ...]
    required: bool
    deadline_text: str
    recurring: bool
    #: One of the task's executions is completed.
    completed: bool = False
    #: The task's version when the row was read: the precondition for moving it.
    version: int = 0
    #: It has a saved time slot (completion is recorded on one, as on the Day page).
    scheduled: bool = False

    @property
    def status_text(self) -> str:
        """Completion in words, so it never depends on color."""
        return "✓ Completed" if self.completed else "○ Not completed"

    @property
    def movable(self) -> bool:
        """An incomplete task can be given a date here; a repeating rule is dated by how it repeats."""
        return not self.completed and not self.recurring

    @property
    def clickable(self) -> bool:
        """A task can be finished, reopened or moved from its row; a repeating rule itself cannot."""
        return not self.recurring


@dataclass(frozen=True)
class MilestoneRow:
    id: uuid.UUID
    number: int
    title: str
    description: str
    score: int

    @property
    def band(self) -> ScoreBand:
        return milestone_score_band(self.score)


@dataclass(frozen=True)
class ProjectsSnapshot:
    projects: list[ProjectRow]
    selected: ProjectRow | None
    tasks: list[ProjectTaskRow] = field(default_factory=list)
    #: Live tasks with no project (for the overview line).
    unassigned_count: int = 0
    #: The selected project's milestones, ascending by number (equal numbers in the order they were added).
    milestones: list[MilestoneRow] = field(default_factory=list)

    @property
    def ongoing(self) -> list[ProjectRow]:
        return [row for row in self.projects if not row.completed]

    @property
    def completed(self) -> list[ProjectRow]:
        return [row for row in self.projects if row.completed]


class ProjectsController:
    def __init__(self, planning: PlanningController, *, timezone: str, executions=None) -> None:
        self._planning = planning
        self.timezone = timezone
        #: The execution controller (completion is the Day page's own record); None: completion is read-only.
        self._executions = executions

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load(self, selected: uuid.UUID | None = None) -> ControllerResult[ProjectsSnapshot]:
        projects = self._planning.list_projects()
        tasks = self._planning.list_tasks()
        if not projects.ok or not tasks.ok:
            return ControllerResult.failure(projects.error or tasks.error)
        by_project: dict[uuid.UUID, list[Task]] = {}
        for task in tasks.value:
            if task.project_id is not None:
                by_project.setdefault(task.project_id, []).append(task)
        placements = self._planning.placements_for_tasks([task.id for task in tasks.value if task.project_id])
        if not placements.ok:
            return ControllerResult.failure(placements.error)
        placed = placements.value
        rows = sorted(
            (ProjectRow(project.id, project.name, project.description or "", project.version,
                        len(by_project.get(project.id, [])),
                        sum(1 for task in by_project.get(project.id, []) if placed.get(task.id)),
                        start_date=project.start_date, estimated_end_date=project.estimated_end_date,
                        completed=project.is_completed, task_defaults=project.task_defaults)
             for project in projects.value),
            key=lambda row: (row.name.lower(), str(row.id)))
        chosen = next((row for row in rows if row.id == selected), None)
        task_rows: list[ProjectTaskRow] = []
        milestones: list[MilestoneRow] = []
        if chosen is not None:
            own = by_project.get(selected, [])
            statuses = self._planning.execution_statuses_for_tasks([task.id for task in own])
            if not statuses.ok:
                return ControllerResult.failure(statuses.error)
            done = {task_id for task_id, found in statuses.value.items() if ExecutionStatus.COMPLETED.value in found}
            task_rows = [self._task_row(task, placed.get(task.id, []), task.id in done) for task in own]
            project = next(project for project in projects.value if project.id == selected)
            milestones = [MilestoneRow(m.id, m.number, m.title, m.description, m.score)
                          for m in project.ordered_milestones]
        task_rows.sort(key=lambda row: (row.dates[0] if row.dates else date_.max, row.name.lower()))
        return ControllerResult.success(ProjectsSnapshot(
            projects=rows, selected=chosen, tasks=task_rows, milestones=milestones,
            unassigned_count=sum(1 for task in tasks.value if task.project_id is None)))

    def _task_row(self, task: Task, placements: list[ScheduledTask], completed: bool = False) -> ProjectTaskRow:
        planned = task_planned_date(task)
        if placements:
            status = "; ".join(
                f"{day_label(p.planned_date)} "
                f"{format_clock(local_minutes(p.planned_start, p.planned_date, p.timezone))} – "
                f"{format_clock(local_minutes(p.planned_end, p.planned_date, p.timezone) or 1440)}"
                for p in placements)
            dates = tuple(dict.fromkeys(p.planned_date for p in placements))
        else:
            status = "Not scheduled"
            dates = (planned,) if planned is not None else ()
        deadline = ""
        if task.deadline is not None:
            from zoneinfo import ZoneInfo

            local = task.deadline.astimezone(ZoneInfo(self.timezone))
            deadline = f"{day_label(local.date())} {format_clock(local.hour * 60 + local.minute)}"
        return ProjectTaskRow(
            task_id=task.id, name=task.name, date_text=day_label(planned) if planned else "Any date",
            duration_text=format_duration(task.estimated_duration_minutes), status=status, dates=dates,
            required=task.required, deadline_text=deadline, recurring=task.recurrence is not None,
            completed=completed, version=task.version, scheduled=bool(placements))

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(name: str, description: str) -> tuple[str, str | None]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A project needs a name.")
        if len(name) > 200:
            raise ValueError("Keep the project name under 200 characters.")
        return name, (description or "").strip() or None

    @staticmethod
    def _dates(start_date: str | date_ | None, estimated_end_date: str | date_ | None
               ) -> tuple[date_ | None, date_ | None]:
        """The typed span (YYYY-MM-DD; empty: not set); ValueError names what is wrong."""
        def read(value, label: str) -> date_ | None:
            if isinstance(value, date_) or value is None:
                return value
            if not value.strip():
                return None
            try:
                return parse_date(value)
            except FieldError as error:
                raise ValueError(f"{label}: {error}") from None

        start, end = read(start_date, "Start date"), read(estimated_end_date, "Estimated end date")
        if start is not None and end is not None and end < start:
            raise ValueError("The estimated end date cannot be before the start date.")
        return start, end

    def create(self, name: str, description: str = "", start_date: str | date_ | None = None,
               estimated_end_date: str | date_ | None = None) -> ControllerResult[Project]:
        try:
            name, text = self._clean(name, description)
            start, end = self._dates(start_date, estimated_end_date)
        except ValueError as error:
            return ControllerResult.failure(str(error), error)
        return self._planning.create_project(
            Project(name=name, description=text, start_date=start, estimated_end_date=end))

    def update(self, project_id: uuid.UUID, name: str, description: str, *, expected_version: int,
               start_date: str | date_ | None = None, estimated_end_date: str | date_ | None = None,
               set_dates: bool = False, task_defaults: tuple[str, str, str] | None = None
               ) -> ControllerResult[Project]:
        """
        Rename / re-describe a project; with set_dates also replace its planned
        span (empty: cleared), and with task_defaults -- the typed default
        (duration, priority, points), each empty for "not set" -- the defaults
        of its future tasks. Existing tasks are never changed by this.
        """
        try:
            name, text = self._clean(name, description)
            changes: dict = {"name": name, "description": text}
            if set_dates:
                changes["start_date"], changes["estimated_end_date"] = self._dates(start_date, estimated_end_date)
            if task_defaults is not None:
                changes["task_defaults"] = parse_project_defaults(*task_defaults).model_dump()
        except ValueError as error:
            return ControllerResult.failure(str(error), error)
        return self._change(project_id, lambda stored: changes, expected_version=expected_version)

    def _change(self, project_id: uuid.UUID, changes, *, expected_version: int | None = None
                ) -> ControllerResult[Project]:
        """
        Save the stored project with `changes(stored)` applied, version-checked:
        against `expected_version`, or the version just read (a change made
        elsewhere in between is refused, never overwritten).
        """
        stored = self._planning.get_project(project_id)
        if not stored.ok:
            return ControllerResult.failure(stored.error)
        if stored.value is None:
            return ControllerResult.failure("That project no longer exists.")
        try:
            updated = Project.model_validate({**stored.value.model_dump(), **changes(stored.value)})
        except ValidationError as error:
            details = error.errors()
            message = str(details[0].get("msg", error)).removeprefix("Value error, ") if details else str(error)
            return ControllerResult.failure(message, error)
        version = stored.value.version if expected_version is None else expected_version
        return self._planning.update_project(updated, expected_version=version)

    def set_completed(self, project_id: uuid.UUID, completed: bool, *, expected_version: int
                      ) -> ControllerResult[Project]:
        """Mark a project complete, or reopen it. Its tasks and milestones are left as they are."""
        def changes(stored: Project) -> dict:
            if completed == stored.is_completed:
                return {}
            return {"completed_at": datetime.now(timezone.utc) if completed else None}

        return self._change(project_id, changes, expected_version=expected_version)

    # ------------------------------------------------------------------
    # Milestones
    # ------------------------------------------------------------------

    def add_milestone(self, project_id: uuid.UUID, number: str | int, title: str, description: str = ""
                      ) -> ControllerResult[Project]:
        """Add a milestone (score 1). The number must be a whole number; the title is required."""
        text = str(number).strip()
        try:
            value = int(text)
        except ValueError:
            return ControllerResult.failure(
                "Enter the milestone number." if not text else f"“{text}” is not a whole number.")
        title = (title or "").strip()
        if not title:
            return ControllerResult.failure("A milestone needs a title.")
        if len(title) > 200:
            return ControllerResult.failure("Keep the milestone title under 200 characters.")
        description = (description or "").strip()
        if not description:
            return ControllerResult.failure("Describe the milestone.")
        try:
            milestone = ProjectMilestone(number=value, title=title, description=description)
        except ValidationError:
            return ControllerResult.failure("That milestone number or description is too long.")
        return self._change(project_id, lambda stored: {"milestones": [*stored.milestones, milestone]})

    def set_milestone_score(self, project_id: uuid.UUID, milestone_id: uuid.UUID, score: int
                            ) -> ControllerResult[Project]:
        if isinstance(score, bool) or not isinstance(score, int) \
                or not MIN_MILESTONE_SCORE <= score <= MAX_MILESTONE_SCORE:
            return ControllerResult.failure(
                f"Choose a score from {MIN_MILESTONE_SCORE} to {MAX_MILESTONE_SCORE}.")

        def changes(stored: Project) -> dict:
            if all(milestone.id != milestone_id for milestone in stored.milestones):
                raise LookupError
            return {"milestones": [milestone.model_copy(update={"score": score}) if milestone.id == milestone_id
                                   else milestone for milestone in stored.milestones]}

        try:
            return self._change(project_id, changes)
        except LookupError:
            return ControllerResult.failure("That milestone no longer exists.")

    def remove_milestone(self, project_id: uuid.UUID, milestone_id: uuid.UUID) -> ControllerResult[Project]:
        def changes(stored: Project) -> dict:
            if all(milestone.id != milestone_id for milestone in stored.milestones):
                raise LookupError
            return {"milestones": [milestone for milestone in stored.milestones if milestone.id != milestone_id]}

        try:
            return self._change(project_id, changes)
        except LookupError:
            return ControllerResult.failure("That milestone no longer exists.")

    # ------------------------------------------------------------------
    # The project's tasks
    # ------------------------------------------------------------------

    def _form(self, day: date_) -> SchedulePageController:
        """The shared task-form presenter (validation, choices, saving) for one date."""
        return SchedulePageController(self._planning, number_of_days=1, anchor_date=day, timezone=self.timezone)

    def today(self) -> date_:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(self.timezone)).date()

    def editor_options(self) -> ControllerResult[EditorOptions]:
        """The task form's choices in this workspace (categories, dependencies, task types)."""
        return self._form(self.today()).editor_options()

    def add_task(self, project_id: uuid.UUID, draft: TaskDraft, date_text: str) -> ControllerResult[PageSnapshot]:
        """
        Add the form's task to the project on the chosen date, exactly as a
        task added on that date's Week/Month selection is: saved with the date
        and no time slot (nothing is scheduled). A failure's cause is
        FormErrors (field -> message) where a field is to blame.
        """
        try:
            if not (date_text or "").strip():
                raise FieldError("Choose the date this task is added to.")
            day = parse_date(date_text)
        except FieldError as error:
            return ControllerResult.failure(str(error), FormErrors({"date": str(error)}))
        draft = replace(draft, kind="task", project_id=project_id, date=day.isoformat())
        if not draft.duration.strip():
            # Nothing typed: the project's own configured duration, when it has one (the form's defaults button
            # offers the category's otherwise; an unset project value never stands in for it).
            stored = self._planning.get_project(project_id)
            configured = stored.value.task_defaults.duration_minutes if stored.ok and stored.value else None
            if configured is not None:
                draft = replace(draft, duration=format_duration(configured))
        return self._form(day).save_draft(draft)

    def task_removal(self, task_id: uuid.UUID) -> ControllerResult[tuple[str, list[tuple[str, str]]]]:
        """(what removing the task does, in words; its scope choices -- several only for a repeating task)."""
        form, ref = self._form(self.today()), RowRef("task", task_id)
        description = form.delete_description(ref)
        if not description.ok:
            return ControllerResult.failure(description.error, description.cause)
        choices = form.removal_choices(ref)
        if not choices.ok:
            return ControllerResult.failure(choices.error, choices.cause)
        return ControllerResult.success((description.value, choices.value))

    def remove_task(self, task_id: uuid.UUID, *, expected_version: int, scope: str | None = None
                    ) -> ControllerResult[PageSnapshot]:
        """Remove a task exactly as the schedule pages do (its saved placements go with it; history is kept)."""
        return self._form(self.today()).delete(RowRef("task", task_id, expected_version), scope=scope)

    def set_task_completed(self, task_id: uuid.UUID, completed: bool) -> ControllerResult[bool]:
        """
        Mark a project's task finished, or not finished again (see the module
        docstring). On its saved time slot(s) when it has any -- exactly as its
        card on the Day page does -- and otherwise directly, without one.
        Idempotent in both directions; a task outside every project keeps the
        rule that it is completed on a scheduled slot.
        """
        if self._executions is None:
            return ControllerResult.failure("Completion cannot be changed here.")
        stored = self._planning.get_task(task_id)
        placements = self._planning.placements_for_tasks([task_id])
        if not stored.ok or not placements.ok:
            return ControllerResult.failure(stored.error or placements.error)
        task = stored.value
        if task is None:
            return ControllerResult.failure("That task no longer exists.")
        if task.is_series:
            return ControllerResult.failure("A repeating task is completed one occurrence at a time.")
        slots = placements.value.get(task_id, [])
        if not slots and task.project_id is None:
            return ControllerResult.failure(
                "This task has no time slot yet, and a task outside a project is finished on its scheduled slot.")
        if slots:
            result = self._executions.set_outcomes(
                [(task, placement) for placement in slots],
                TaskOutcome.COMPLETED if completed else TaskOutcome.PENDING)
            if not result.ok:
                return ControllerResult.failure(result.error, result.cause)
            cancelled_only = not result.value.changed and not result.value.unchanged
            if completed and not cancelled_only:
                return ControllerResult.success(True)
            if completed and task.project_id is None:
                return ControllerResult.failure("Its scheduled attempt was cancelled and cannot be changed.")
        if completed:
            done = self._executions.complete_directly(task)
            return ControllerResult.success(True) if done.ok else ControllerResult.failure(done.error, done.cause)
        # Undo: whichever way it was completed (a slot's completion was reopened above).
        undone = self._executions.reopen_directly(task_id)
        return ControllerResult.success(False) if undone.ok else ControllerResult.failure(undone.error, undone.cause)

    def assign_date(self, task_id: uuid.UUID, day: date_, *, expected_version: int) -> ControllerResult[Task]:
        """
        Give an existing task the date `day` (as changing the date in its task
        form does) and remove its saved placements: the same task, waiting on
        that date without a time slot. Version-checked; on a failure nothing
        changed.
        """
        stored = self._planning.get_task(task_id)
        if not stored.ok:
            return ControllerResult.failure(stored.error)
        task = stored.value
        if task is None:
            return ControllerResult.failure("That task no longer exists.")
        if task.is_series:
            return ControllerResult.failure("A repeating task is dated by how it repeats; move one of its occurrences.")
        try:
            moved = build_task(replace(draft_from_task(task, self.timezone), date=day.isoformat()),
                               timezone_name=self.timezone, existing=task)
        except FormErrors as errors:
            return ControllerResult.failure("; ".join(errors.errors.values()), errors)
        if task_planned_date(moved) != day:  # other preferred dates never outrank the chosen one
            moved = moved.model_copy(update={"preferred_dates": [day]})
        return self._planning.move_task_unscheduled(moved, expected_version=expected_version)

    def delete(self, project_id: uuid.UUID, *, expected_version: int) -> ControllerResult[bool]:
        """Delete an empty project; one still in use is refused, naming its tasks (nothing is cascaded)."""
        result = self._planning.delete_project(project_id, expected_version=expected_version)
        if result.ok or not isinstance(result.cause, EntityInUseError):
            return result
        names = self._planning.get_tasks(list(result.cause.dependent_ids))
        listed = ", ".join(sorted(task.name for task in names.value.tasks.values())) if names.ok else ""
        return ControllerResult.failure(
            f"This project still has {len(result.cause.dependent_ids)} task(s)"
            + (f": {listed}" if listed else "")
            + ". Nothing was deleted. Move those tasks to another project (or to no project) first.",
            result.cause)

    def reassign_tasks(self, from_project: uuid.UUID, to_project: uuid.UUID | None) -> ControllerResult[int]:
        """Move every live task of `from_project` to `to_project` (None: no project), atomically, version-checked."""
        tasks = self._planning.list_tasks()
        if not tasks.ok:
            return ControllerResult.failure(tasks.error)
        moving = [task for task in tasks.value if task.project_id == from_project]
        if not moving:
            return ControllerResult.success(0)
        saved = self._planning.add_or_update_tasks(
            [task.model_copy(update={"project_id": to_project}) for task in moving],
            expected_versions={task.id: task.version for task in moving})
        if not saved.ok:
            return ControllerResult.failure(f"{saved.error} Nothing was moved.", saved.cause)
        return ControllerResult.success(len(moving))
