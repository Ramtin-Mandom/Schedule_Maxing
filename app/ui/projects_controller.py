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
- **Archive:** there is none, so none is offered.
- **The project's schedule:** its tasks, by id, with their planned dates and
  every saved placement (date and exact times), or "not scheduled".
  Recurring tasks keep their rule and are shown once; they are not expanded.
- **Owner scope:** everything follows the controller's workspace. New
  projects in an account workspace belong to that account.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as date_

from app.planning.application import task_planned_date
from app.planning.errors import EntityInUseError
from app.planning.models import Project, ScheduledTask, Task
from app.planning.time import local_minutes
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import day_label
from app.ui.time_fields import format_clock, format_duration


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

    @property
    def label(self) -> str:
        return (f"{self.name} — {self.task_count} task(s), {self.scheduled_count} scheduled"
                if self.task_count else f"{self.name} — no tasks")


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


@dataclass(frozen=True)
class ProjectsSnapshot:
    projects: list[ProjectRow]
    selected: ProjectRow | None
    tasks: list[ProjectTaskRow] = field(default_factory=list)
    #: Live tasks with no project (for the overview line).
    unassigned_count: int = 0


class ProjectsController:
    def __init__(self, planning: PlanningController, *, timezone: str) -> None:
        self._planning = planning
        self.timezone = timezone

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
                        sum(1 for task in by_project.get(project.id, []) if placed.get(task.id)))
             for project in projects.value),
            key=lambda row: (row.name.lower(), str(row.id)))
        chosen = next((row for row in rows if row.id == selected), None)
        task_rows = [self._task_row(task, placed.get(task.id, [])) for task in by_project.get(selected, [])] \
            if chosen is not None else []
        task_rows.sort(key=lambda row: (row.dates[0] if row.dates else date_.max, row.name.lower()))
        return ControllerResult.success(ProjectsSnapshot(
            projects=rows, selected=chosen, tasks=task_rows,
            unassigned_count=sum(1 for task in tasks.value if task.project_id is None)))

    def _task_row(self, task: Task, placements: list[ScheduledTask]) -> ProjectTaskRow:
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
            required=task.required, deadline_text=deadline, recurring=task.recurrence is not None)

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

    def create(self, name: str, description: str = "") -> ControllerResult[Project]:
        try:
            name, text = self._clean(name, description)
        except ValueError as error:
            return ControllerResult.failure(str(error), error)
        return self._planning.create_project(Project(name=name, description=text))

    def update(self, project_id: uuid.UUID, name: str, description: str, *, expected_version: int
               ) -> ControllerResult[Project]:
        try:
            name, text = self._clean(name, description)
        except ValueError as error:
            return ControllerResult.failure(str(error), error)
        stored = self._planning.get_project(project_id)
        if not stored.ok:
            return ControllerResult.failure(stored.error)
        if stored.value is None:
            return ControllerResult.failure("That project no longer exists.")
        updated = stored.value.model_copy(update={"name": name, "description": text})
        return self._planning.update_project(updated, expected_version=expected_version)

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
