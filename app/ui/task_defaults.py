"""
app/ui/task_defaults.py

The task form's "Use default values" and the user's own categories, Tk-free:
for "general" (no category chosen) and for every category, the name,
duration, priority and points the form fills in, plus the categories added
in Settings. Kept in a small JSON file next to the application database
(task_defaults.json), like the appearance settings (app/ui/ui_settings.py):
on this device only, never synchronized.

Out of the box every set is priority 5, 60 minutes and 20 points, named
after its category ("Task" for general); only what the user changed or added
is stored. reset() -- run by Settings' "Reset All Task Data" -- removes the
added categories and every changed value. A missing, unreadable or
hand-edited file never prevents startup: invalid entries are dropped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.planning.models import MAX_TASK_POINTS, ProjectTaskDefaults
from app.ui.task_form_model import CATEGORIES, PRIORITIES, parse_points
from app.ui.time_fields import FieldError, parse_duration

logger = logging.getLogger(__name__)

FILENAME = "task_defaults.json"
GENERAL_NAME = "Task"
MAX_CATEGORY_LENGTH = 40


@dataclass(frozen=True)
class TaskDefault:
    name: str
    duration: int = 60
    priority: int = 5
    points: int = 20


def resolve_task_default(category_default: TaskDefault, project: ProjectTaskDefaults | None) -> TaskDefault:
    """
    The defaults of a new task of a project: each value the project
    explicitly configured, else the category's (which is the application's
    own where the category was never changed). An unset project value never
    replaces anything.
    """
    if project is None:
        return category_default
    return replace(
        category_default,
        duration=project.duration_minutes if project.duration_minutes is not None else category_default.duration,
        priority=project.priority if project.priority is not None else category_default.priority,
        points=project.points if project.points is not None else category_default.points,
    )


def parse_project_defaults(duration: str, priority: str, points: str) -> ProjectTaskDefaults:
    """A project's task defaults as typed (each empty: not set); ValueError says what is wrong."""
    try:
        minutes = parse_duration(duration) if (duration or "").strip() else None
        value = parse_points(points) if (points or "").strip() else None
    except FieldError as error:
        raise ValueError(str(error)) from None
    chosen = (priority or "").strip()
    if chosen and chosen not in PRIORITIES:
        raise ValueError("Choose a default priority from 1 (low) to 10 (high), or leave it empty.")
    return ProjectTaskDefaults(duration_minutes=minutes, priority=int(chosen) if chosen else None, points=value)


def parse_default(name: str, duration: str, priority: str, points: str) -> TaskDefault:
    """A set of default values as typed in Settings; ValueError says what is wrong."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise ValueError("Enter a default name.")
    if str(priority) not in PRIORITIES:
        raise ValueError("Choose a priority from 1 (low) to 10 (high).")
    try:
        return TaskDefault(cleaned, parse_duration(duration), int(priority), parse_points(points))
    except FieldError as error:
        raise ValueError(str(error)) from None


@dataclass(frozen=True)
class TaskDefaults:
    #: The changed general set (None: the built-in one).
    general: TaskDefault | None = None
    #: Category -> its changed set (a category without one uses the built-in set, named after it).
    categories: dict[str, TaskDefault] = field(default_factory=dict)
    #: The categories added in Settings, in the order they were added.
    custom_categories: tuple[str, ...] = ()

    def all_categories(self) -> list[str]:
        return [*CATEGORIES, *(name for name in self.custom_categories if name not in CATEGORIES)]

    def for_category(self, category: str | None) -> TaskDefault:
        """The values "Use default values" fills in (`category` None or "": the general set)."""
        if not category:
            return self.general or TaskDefault(GENERAL_NAME)
        return self.categories.get(category) or TaskDefault(category[:1].upper() + category[1:])

    def with_default(self, category: str | None, default: TaskDefault) -> "TaskDefaults":
        if not category:
            return replace(self, general=default)
        return replace(self, categories={**self.categories, category: default})

    def with_category(self, name: str) -> "TaskDefaults":
        """With one more category; ValueError for an empty, too long or existing name."""
        cleaned = " ".join((name or "").split())
        if not cleaned:
            raise ValueError("Enter the new category's name.")
        if len(cleaned) > MAX_CATEGORY_LENGTH:
            raise ValueError(f"A category name has at most {MAX_CATEGORY_LENGTH} characters.")
        if cleaned.lower() in {existing.lower() for existing in self.all_categories()}:
            raise ValueError(f"The category “{cleaned}” already exists.")
        return replace(self, custom_categories=(*self.custom_categories, cleaned))

    def without_category(self, name: str) -> "TaskDefaults":
        """Without an added category and its values (tasks already saved with it keep it)."""
        if name not in self.custom_categories:
            raise ValueError("Only a category you added can be removed.")
        return replace(self, custom_categories=tuple(c for c in self.custom_categories if c != name),
                       categories={key: value for key, value in self.categories.items() if key != name})


def defaults_path_for(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / FILENAME


def _read_default(data: object) -> TaskDefault | None:
    if not isinstance(data, dict):
        return None
    name, duration, priority, points = (data.get(key) for key in ("name", "duration", "priority", "points"))
    valid = (isinstance(name, str) and name.strip()
             and all(isinstance(value, int) and not isinstance(value, bool) for value in (duration, priority, points))
             and 1 <= duration <= 1440 and 1 <= priority <= 10 and 0 <= points <= MAX_TASK_POINTS)
    return TaskDefault(name.strip(), duration, priority, points) if valid else None


class TaskDefaultsStore:
    """The current TaskDefaults (`value`) and its file; without a path it only lasts as long as the store."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.value = self._load()

    def _load(self) -> TaskDefaults:
        if self.path is None:
            return TaskDefaults()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TaskDefaults()
        except (OSError, ValueError) as error:
            logger.warning("Ignoring unreadable task defaults %s: %s", self.path, error)
            return TaskDefaults()
        if not isinstance(data, dict):
            return TaskDefaults()
        custom = data.get("custom_categories")
        stored = data.get("categories")
        categories = {name: default for name, default in
                      ((name, _read_default(raw)) for name, raw in (stored.items() if isinstance(stored, dict) else ()))
                      if default is not None}
        return TaskDefaults(
            general=_read_default(data.get("general")), categories=categories,
            custom_categories=tuple(dict.fromkeys(
                name.strip() for name in (custom if isinstance(custom, list) else ())
                if isinstance(name, str) and name.strip() and name.strip() not in CATEGORIES)),
        )

    def save(self, value: TaskDefaults) -> bool:
        """Use `value` from now on and write it atomically; False (logged) if the file cannot be written."""
        self.value = value
        if self.path is None:
            return True
        data = {"general": value.general.__dict__ if value.general else None,
                "categories": {name: default.__dict__ for name, default in value.categories.items()},
                "custom_categories": list(value.custom_categories)}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            logger.warning("Could not save task defaults to %s: %s", self.path, error)
            return False
        return True

    def reset(self) -> bool:
        """Back to the built-in values: every added category and changed value is removed."""
        return self.save(TaskDefaults())
