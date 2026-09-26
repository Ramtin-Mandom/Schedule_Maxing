"""
app/ui/task_form_model.py

The reusable task form's model (Milestone 4, Prompt 3), Tk-free: what the
form shows (a TaskDraft of plain values, never ids, owners, timestamps or
versions to type) and how it becomes a canonical Task or FixedBlock
(app/planning/models.py). Day, Week, Month and later Projects use the same
draft and the same rules.

Flexible task fields: name, category, estimated duration (minutes, typed
freely), priority 1-10, the date it is planned for (optional: an undated
task can go on any date), "pin to this date" (required_date), "required"
(must be scheduled), a preferred time window, a deadline (date and time),
dependencies, a project and ordered tags.

Fixed block fields: label, category, date, start and end. A fixed block is
not a disguised flexible task, and neither kind is converted into the other.

Edits start from the stored model and change only the form's fields, so
recurrence, further preferred dates, tags order, dependencies, the project,
an unknown imported category and anything the form does not show survive.
A category the form does not list stays selectable for that record.

Times are exact minutes in the planning timezone and are converted with the
planning time helpers, which refuse -- with a message, never by rounding --
a time skipped or repeated by a daylight-saving change and an interval
crossing one. A window or block may end at the following midnight; an
overnight interval (ending after the next midnight, or ending before it
starts) is refused. Validation that needs stored data (fixed-block overlap,
the day window, references, versions, owners) stays in the services.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date as date_
from typing import Literal

from pydantic import ValidationError

from app.planning.models import FixedBlock, LocalTimeWindow, Task
from app.planning.time import (
    AmbiguousLocalTimeError,
    LocalDayWindow,
    UnsupportedSchedulingWindowError,
    local_instant,
    local_minutes,
)
from app.ui.time_fields import (
    MINUTES_PER_DAY,
    FieldError,
    format_clock,
    format_date,
    format_duration,
    parse_clock,
    parse_date,
    parse_duration,
)

#: The categories every form offers (a stored category outside this list is added for that record).
CATEGORIES = ["study", "work", "class", "exercise", "sleep", "food", "event", "entertainment", "errand", "other"]
PRIORITIES = [str(value) for value in range(1, 11)]
MAX_TAG_LENGTH = 60


class FormErrors(ValueError):
    """Field -> message for everything wrong with a draft (nothing was saved)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(" ".join(errors.values()))
        self.errors = errors


@dataclass(frozen=True)
class TaskDraft:
    kind: Literal["task", "block"] = "task"
    name: str = ""
    category: str = "study"
    #: ISO date, or "" for an undated flexible task.
    date: str = ""
    # -- flexible task ---------------------------------------------------------------
    duration: str = ""
    priority: str = "5"
    required: bool = False
    pin_to_date: bool = False
    window_start: str = ""
    window_end: str = ""
    deadline_date: str = ""
    deadline_time: str = ""
    dependency_ids: tuple[uuid.UUID, ...] = ()
    project_id: uuid.UUID | None = None
    tags: tuple[str, ...] = ()
    # -- fixed block -----------------------------------------------------------------
    start: str = ""
    end: str = ""

    def with_tag(self, tag: str) -> "TaskDraft":
        """The draft with `tag` appended (trimmed; empty and repeated tags are ignored); raises FieldError if too long."""
        cleaned = " ".join(tag.split())
        if not cleaned or cleaned in self.tags:
            return self
        if len(cleaned) > MAX_TAG_LENGTH:
            raise FieldError(f"A tag has at most {MAX_TAG_LENGTH} characters.")
        return replace(self, tags=(*self.tags, cleaned))

    def without_tag(self, tag: str) -> "TaskDraft":
        return replace(self, tags=tuple(existing for existing in self.tags if existing != tag))


def categories_for(current: str | None) -> list[str]:
    """The category choices, with a stored category the list lacks kept (never remapped)."""
    return CATEGORIES + ([current] if current and current not in CATEGORIES else [])


# -----------------------------------------------------------------------------
# Stored model -> draft
# -----------------------------------------------------------------------------


def draft_from_task(task: Task, timezone_name: str) -> TaskDraft:
    planned = task.required_date or (task.preferred_dates[0] if task.preferred_dates else None)
    window = task.preferred_time_window
    deadline_date = deadline_time = ""
    if task.deadline is not None:
        local = task.deadline.astimezone(_zone(timezone_name))
        deadline_date, deadline_time = local.date().isoformat(), format_clock(local.hour * 60 + local.minute)
    return TaskDraft(
        kind="task", name=task.name, category=task.category, date=format_date(planned),
        duration=format_duration(task.estimated_duration_minutes), priority=str(task.priority),
        required=task.required, pin_to_date=task.required_date is not None,
        window_start=format_clock(window.start_minute) if window else "",
        window_end=format_clock(window.end_minute) if window else "",
        deadline_date=deadline_date, deadline_time=deadline_time,
        dependency_ids=tuple(task.dependency_ids), project_id=task.project_id, tags=tuple(task.tags),
    )


def draft_from_block(block: FixedBlock) -> TaskDraft:
    start = local_minutes(block.planned_start, block.planned_date, block.timezone)
    end = local_minutes(block.planned_end, block.planned_date, block.timezone)
    return TaskDraft(kind="block", name=block.label, category=block.category, date=format_date(block.planned_date),
                     start=format_clock(start), end=format_clock(end))


def _zone(name: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


# -----------------------------------------------------------------------------
# Draft -> stored model
# -----------------------------------------------------------------------------


def build_task(draft: TaskDraft, *, timezone_name: str, existing: Task | None = None) -> Task:
    """A new Task, or `existing` with the form's fields changed; FormErrors lists every problem."""
    errors: dict[str, str] = {}
    name = draft.name.strip()
    if not name:
        errors["name"] = "Enter a name."
    if not draft.category.strip():
        errors["category"] = "Choose a category."

    duration = _field(errors, "duration", lambda: parse_duration(draft.duration))
    priority = None
    if draft.priority not in PRIORITIES:
        errors["priority"] = "Choose a priority from 1 (low) to 10 (high)."
    else:
        priority = int(draft.priority)

    planned: date_ | None = None
    if draft.date.strip():
        planned = _field(errors, "date", lambda: parse_date(draft.date))
    elif draft.pin_to_date:
        errors["date"] = "Choose the date this task is pinned to."

    window = None
    if draft.window_start.strip() or draft.window_end.strip():
        start = _field(errors, "window_start", lambda: parse_clock(draft.window_start))
        end = _field(errors, "window_end", lambda: parse_clock(draft.window_end, end_of_interval=True))
        if start is not None and end is not None:
            if end <= start:
                errors["window_end"] = _overnight_message("preferred window")
            else:
                window = LocalTimeWindow(start_minute=start, end_minute=end)

    deadline = None
    if draft.deadline_date.strip() or draft.deadline_time.strip():
        if not draft.deadline_date.strip():
            errors["deadline_date"] = "Add the deadline's date."
        elif not draft.deadline_time.strip():
            errors["deadline_time"] = "Add the deadline's time, like 5:00 PM."
        else:
            day = _field(errors, "deadline_date", lambda: parse_date(draft.deadline_date))
            minute = _field(errors, "deadline_time", lambda: parse_clock(draft.deadline_time))
            if day is not None and minute is not None:
                deadline = _field(errors, "deadline_time", lambda: _instant(day, minute, timezone_name, "deadline"))

    if errors:
        raise FormErrors(errors)

    tags = [" ".join(tag.split()) for tag in draft.tags if tag.strip()]
    fields: dict = dict(
        name=name, category=draft.category.strip(), estimated_duration_minutes=duration, priority=priority,
        required=draft.required, preferred_time_window=window, deadline=deadline,
        dependency_ids=list(dict.fromkeys(draft.dependency_ids)), project_id=draft.project_id, tags=tags,
    )
    previous_dates = list(existing.preferred_dates) if existing is not None else []
    if draft.pin_to_date:
        fields["required_date"] = planned
        fields["preferred_dates"] = previous_dates
    else:
        fields["required_date"] = None
        # Replace the date represented by the form, preserving additional preferences.
        # Explicitly clearing Date means "any date", so remove all preferred dates.
        previous_date = (existing.required_date or (previous_dates[0] if previous_dates else None)) \
            if existing is not None else None
        fields["preferred_dates"] = ([planned] + [day for day in previous_dates if day not in (previous_date, planned)]) \
            if planned else []
    try:
        if existing is None:
            return Task(**fields)
        data = existing.model_dump()
        data.update(fields)
        return Task.model_validate(data)
    except ValidationError as error:
        raise FormErrors({"form": _first_message(error)}) from None


def build_block(draft: TaskDraft, *, timezone_name: str, existing: FixedBlock | None = None) -> FixedBlock:
    """A new FixedBlock, or `existing` changed; its times are in its own timezone (the page's for a new one)."""
    errors: dict[str, str] = {}
    label = draft.name.strip()
    if not label:
        errors["name"] = "Enter a label."
    if not draft.category.strip():
        errors["category"] = "Choose a category."
    day = _field(errors, "date", lambda: parse_date(draft.date)) if draft.date.strip() else None
    if not draft.date.strip():
        errors["date"] = "Choose the block's date."
    start = _field(errors, "start", lambda: parse_clock(draft.start))
    end = _field(errors, "end", lambda: parse_clock(draft.end, end_of_interval=True))
    if start is not None and end is not None and end <= start:
        errors["end"] = _overnight_message("fixed block")
    tz_name = existing.timezone if existing is not None else timezone_name
    instants = None
    if not errors:
        try:
            instants = LocalDayWindow(day=day, tz_name=tz_name, start_minute=start, end_minute=end).to_utc_instants()
        except AmbiguousLocalTimeError as error:
            field_name = "start" if _mentions_minute(str(error), start) else "end"
            errors[field_name] = _dst_message(str(error), tz_name)
        except UnsupportedSchedulingWindowError:
            errors["end"] = (f"This block crosses a daylight-saving change in {tz_name}; split it into two blocks "
                             "at the change.")
    if errors:
        raise FormErrors(errors)
    fields = dict(label=label, category=draft.category.strip(), planned_date=day, timezone=tz_name,
                  planned_start=instants[0], planned_end=instants[1])
    try:
        return existing.model_copy(update=fields) if existing is not None else FixedBlock(**fields)
    except ValidationError as error:
        raise FormErrors({"form": _first_message(error)}) from None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _field(errors: dict[str, str], name: str, read):
    try:
        return read()
    except (FieldError, AmbiguousLocalTimeError, ValueError) as error:
        errors.setdefault(name, str(error))
        return None


def _instant(day: date_, minute: int, tz_name: str, what: str):
    try:
        return local_instant(day, minute, tz_name)
    except AmbiguousLocalTimeError as error:
        raise FieldError(_dst_message(str(error), tz_name, what)) from None


def _dst_message(detail: str, tz_name: str, what: str = "time") -> str:
    if "does not exist" in detail:
        return (f"This {what} does not exist on that date in {tz_name}: the clocks skip it for daylight saving. "
                "Choose a time outside the skipped hour.")
    return (f"This {what} happens twice on that date in {tz_name} (the clocks go back for daylight saving). "
            "Choose a time outside the repeated hour.")


def _mentions_minute(detail: str, minute: int | None) -> bool:
    if minute is None or minute >= MINUTES_PER_DAY:
        return False
    return f"T{minute // 60:02d}:{minute % 60:02d}" in detail


def _overnight_message(what: str) -> str:
    return (f"The {what} must end after it starts on the same day. To end at midnight, use 12:00 AM (it means the "
            "next midnight); intervals that continue past midnight are not supported -- split them at midnight.")


def _first_message(error: ValidationError) -> str:
    details = error.errors()
    return str(details[0].get("msg", error)).removeprefix("Value error, ") if details else str(error)


@dataclass(frozen=True)
class Choice:
    """A selectable record, shown by label but always kept by id."""

    id: uuid.UUID
    label: str


@dataclass(frozen=True)
class EditorOptions:
    timezone: str
    categories: list[str] = field(default_factory=lambda: list(CATEGORIES))
    #: Tasks that can be dependencies (never the edited task itself), labelled so duplicate names differ.
    dependencies: list[Choice] = field(default_factory=list)
    projects: list[Choice] = field(default_factory=list)
