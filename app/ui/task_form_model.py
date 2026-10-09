"""
app/ui/task_form_model.py

The reusable task form's model (Milestone 4, Prompt 3), Tk-free: what the
form shows (a TaskDraft of plain values, never ids, owners, timestamps or
versions to type) and how it becomes a canonical Task or FixedBlock
(app/planning/models.py). Day, Week, Month and later Projects use the same
draft and the same rules.

Three kinds, chosen in the form and never converted into one another:

Flexible task fields: name, category, estimated duration (minutes, typed
freely), points (the user's own productivity value, 0-1000 -- never the
optimizer's placement score, and never a scheduling priority), the date it
is planned for (the page's selected date; an imported task may be undated),
"pin to this date" (required_date), "required" (must be scheduled), the
preferred time (Early / Mid / Late: a third of the day's schedulable
window), a deadline (date and time), dependencies, a project and ordered
tags.

Fixed block fields: label, category, points, date, start and end.

To Do fields: name, category, points and tags. A To Do is a checklist item:
it has no duration, time or date, and is never scheduled.

Repeating (docs/recurrence.md): "Repeats" makes a task a recurring series
whose start date is the form's date and whose time zone is the page's (an
edited series keeps its own): daily / weekly (weekdays, default the start
date's) / monthly (a day of month, default the start date's; months without
it are skipped), every N, ending never, on a date (inclusive) or after N
occurrences. A series is dated by its rule, so it takes no pin, other
preferred dates or deadline. A stored series without a start date and time
zone (saved before series repeated) needs configuration: the form shows it
and choosing them configures it. An occurrence of a series is edited like a
task; whether the change applies to that occurrence, to it and every later
one, or to the entire series is chosen when it is saved (recurrence_role).

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

from app.planning.models import (
    DEFAULT_TASK_POINTS,
    MAX_TASK_POINTS,
    TODO_PLACEHOLDER_MINUTES,
    FixedBlock,
    PreferredTime,
    RecurrenceFrequency,
    RecurrenceSpec,
    Task,
    TaskKind,
)
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
#: The choices of the default-priority settings (a legacy value no scheduler reads; the task form has no priority).
PRIORITIES = [str(value) for value in range(1, 11)]
#: The form's preferred-time choices (app.planning.models.PreferredTime) and the one a new task starts with.
PREFERRED_TIMES = tuple(value.value for value in PreferredTime)
DEFAULT_PREFERRED_TIME = PreferredTime.MID.value
MAX_TAG_LENGTH = 60
#: The form's "Repeats" choices: "" does not repeat.
REPEATS = ("", "daily", "weekly", "monthly")
REPEAT_ENDS = ("never", "on", "after")
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class FormErrors(ValueError):
    """Field -> message for everything wrong with a draft (nothing was saved)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(" ".join(errors.values()))
        self.errors = errors


@dataclass(frozen=True)
class TaskDraft:
    #: "task" (flexible), "block" (fixed) or "todo" (a checklist item).
    kind: Literal["task", "block", "todo"] = "task"
    name: str = ""
    category: str = "study"
    #: ISO date, or "" for an undated flexible task.
    date: str = ""
    #: Every kind: what completing it is worth.
    points: str = str(DEFAULT_TASK_POINTS)
    # -- flexible task ---------------------------------------------------------------
    duration: str = ""
    required: bool = False
    pin_to_date: bool = False
    #: "early", "mid" or "late": the preferred third of the day's schedulable window.
    preferred_time: str = DEFAULT_PREFERRED_TIME
    deadline_date: str = ""
    deadline_time: str = ""
    dependency_ids: tuple[uuid.UUID, ...] = ()
    project_id: uuid.UUID | None = None
    tags: tuple[str, ...] = ()
    #: The reusable task type (docs/productivity-redesign-plan.md): None keeps the stored type, or gives a new
    #: task a type of its own. new_type_label, when set, names a type to create and use instead.
    task_type_id: uuid.UUID | None = None
    new_type_label: str = ""
    # -- repeating (a series; docs/recurrence.md) --------------------------------------
    #: "" (does not repeat), "daily", "weekly" or "monthly".
    repeat: str = ""
    repeat_interval: str = "1"
    #: Weekly: 0 = Monday .. 6 = Sunday (none: the start date's weekday).
    repeat_weekdays: tuple[int, ...] = ()
    #: Monthly: 1-31, or "" for the start date's day.
    repeat_day_of_month: str = ""
    #: "never", "on" (repeat_until, inclusive) or "after" (repeat_count occurrences).
    repeat_end: str = "never"
    repeat_until: str = ""
    repeat_count: str = ""
    #: What the stored record is (set when editing): "task", "series" or "occurrence" (of a series).
    recurrence_role: str = "task"
    #: A short description for an edited series or occurrence (e.g. which series and original date).
    recurrence_note: str = ""
    #: An edited series stored without a start date and time zone: choosing "Repeats" configures it.
    needs_configuration: bool = False
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


def describe_rule(spec: RecurrenceSpec) -> str:
    """A short description of a recurrence rule, e.g. "Repeats every 2 weeks on Mon, Wed, until 2026-06-30"."""
    unit = {"daily": "day", "weekly": "week", "monthly": "month"}[spec.frequency.value]
    text = f"Repeats every {unit}" if spec.interval == 1 else f"Repeats every {spec.interval} {unit}s"
    if spec.frequency == RecurrenceFrequency.WEEKLY:
        days = spec.weekdays or ([spec.start_date.weekday()] if spec.start_date else [])
        if days:
            text += " on " + ", ".join(WEEKDAY_NAMES[day] for day in days)
    if spec.frequency == RecurrenceFrequency.MONTHLY:
        day = spec.day_of_month or (spec.start_date.day if spec.start_date else None)
        if day:
            text += f" on day {day}"
    if spec.start_date is not None:
        text += f", from {spec.start_date.isoformat()} ({spec.timezone})"
    if spec.end_date is not None:
        text += f", until {spec.end_date.isoformat()}"
    elif spec.count is not None:
        text += f", {spec.count} times"
    return text


def draft_from_task(task: Task, timezone_name: str, *, series: Task | None = None) -> TaskDraft:
    """The form's view of a stored task (`series`: an occurrence's series definition, for its description)."""
    if task.is_todo:
        return TaskDraft(kind="todo", name=task.name, category=task.category, points=str(task.points),
                         tags=tuple(task.tags), project_id=task.project_id, task_type_id=task.task_type_id)
    planned = task.required_date or (task.preferred_dates[0] if task.preferred_dates else None)
    deadline_date = deadline_time = ""
    if task.deadline is not None:
        local = task.deadline.astimezone(_zone(timezone_name))
        deadline_date, deadline_time = local.date().isoformat(), format_clock(local.hour * 60 + local.minute)
    recurrence: dict = {}
    spec = task.recurrence
    if spec is not None:
        planned = spec.start_date or planned
        recurrence = dict(
            # A rule that needs configuration is shown, but only choosing "Repeats" again configures it.
            repeat=spec.frequency.value if spec.configured else "",
            repeat_interval=str(spec.interval), repeat_weekdays=tuple(spec.weekdays or ()),
            repeat_day_of_month=str(spec.day_of_month) if spec.day_of_month else "",
            repeat_end="on" if spec.end_date else "after" if spec.count else "never",
            repeat_until=format_date(spec.end_date) if spec.end_date else "",
            repeat_count=str(spec.count) if spec.count else "", recurrence_role="series",
            needs_configuration=not spec.configured,
            recurrence_note=(describe_rule(spec) if spec.configured else
                             "Needs setup: this repeating task was saved before repeating tasks were expanded, so it "
                             "does not repeat yet. Choose how it repeats (it starts on the date shown) to start it."),
        )
    elif task.is_occurrence:
        note = f"One occurrence (originally {task.occurrence_slot.isoformat()})"
        if series is not None:
            note += f" of \u201c{series.name}\u201d"
            if series.recurrence is not None and series.recurrence.configured:
                note += f" -- {describe_rule(series.recurrence).lower()}"
        if task.occurrence_state is not None:
            note += " (edited on its own)"
        recurrence = dict(recurrence_role="occurrence", recurrence_note=note + ".")
    return TaskDraft(
        kind="task", name=task.name, category=task.category, date=format_date(planned),
        duration=format_duration(task.estimated_duration_minutes),
        points=str(task.points),
        required=task.required, pin_to_date=task.required_date is not None,
        preferred_time=task.preferred_time.value if task.preferred_time is not None else DEFAULT_PREFERRED_TIME,
        deadline_date=deadline_date, deadline_time=deadline_time,
        dependency_ids=tuple(task.dependency_ids), project_id=task.project_id, tags=tuple(task.tags),
        task_type_id=task.task_type_id,
        **recurrence,
    )


def draft_from_block(block: FixedBlock) -> TaskDraft:
    start = local_minutes(block.planned_start, block.planned_date, block.timezone)
    end = local_minutes(block.planned_end, block.planned_date, block.timezone)
    return TaskDraft(kind="block", name=block.label, category=block.category, date=format_date(block.planned_date),
                     points=str(block.points), start=format_clock(start), end=format_clock(end))


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
    points = _field(errors, "points", lambda: parse_points(draft.points))
    if draft.preferred_time not in PREFERRED_TIMES:
        errors["preferred_time"] = "Choose a preferred time: Early, Mid or Late."

    planned: date_ | None = None
    if draft.date.strip():
        planned = _field(errors, "date", lambda: parse_date(draft.date))
    elif draft.pin_to_date:
        errors["date"] = "Choose the date this task is pinned to."

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

    recurrence = None
    if draft.repeat:
        if draft.recurrence_role == "occurrence":
            errors["repeat"] = "This is one occurrence of a repeating task; change how it repeats on the series."
        else:
            recurrence = _recurrence(draft, errors, planned, timezone_name, existing)
            if deadline is not None:
                errors["deadline_date"] = "A repeating task is dated by how it repeats: it has no deadline."

    if errors:
        raise FormErrors(errors)

    tags = [" ".join(tag.split()) for tag in draft.tags if tag.strip()]
    fields: dict = dict(
        name=name, category=draft.category.strip(), estimated_duration_minutes=duration,
        points=points, kind=TaskKind.FLEXIBLE,
        required=draft.required, preferred_time=draft.preferred_time, deadline=deadline,
        dependency_ids=list(dict.fromkeys(draft.dependency_ids)), project_id=draft.project_id, tags=tags,
    )
    previous_dates = list(existing.preferred_dates) if existing is not None else []
    if recurrence is not None:
        # A series is dated by its rule.
        fields.update(recurrence=recurrence, required_date=None, preferred_dates=[], deadline=None)
    elif existing is not None and existing.is_series and existing.needs_configuration:
        # Not configured yet and no rule chosen: everything about its recurrence stays exactly as stored.
        fields.update(recurrence=existing.recurrence, required_date=existing.required_date,
                      preferred_dates=list(existing.preferred_dates))
    elif existing is not None and existing.is_series:
        fields["recurrence"] = None  # "Does not repeat" for a configured series (refused while it has occurrences)
    elif draft.pin_to_date:
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
    if draft.task_type_id is not None:
        fields["task_type_id"] = draft.task_type_id  # None: a stored task keeps its type, a new one gets its own
    try:
        if existing is None:
            return Task(**fields)
        data = existing.model_dump()
        data.update(fields)
        return Task.model_validate(data)
    except ValidationError as error:
        raise FormErrors({"form": _first_message(error)}) from None


def build_todo(draft: TaskDraft, *, existing: Task | None = None) -> Task:
    """
    A new To Do, or `existing` (a To Do) with the form's fields changed. Only
    name, category, points and tags are read from the draft, plus -- for a new
    one -- the date it is added for (the day's checklist it belongs to; an
    edited one keeps its own). Nothing about a duration or a time is taken
    from the draft, whatever it carries.
    """
    errors: dict[str, str] = {}
    name = draft.name.strip()
    if not name:
        errors["name"] = "Enter a name."
    if not draft.category.strip():
        errors["category"] = "Choose a category."
    points = _field(errors, "points", lambda: parse_points(draft.points))
    if errors:
        raise FormErrors(errors)
    fields: dict = dict(name=name, category=draft.category.strip(), points=points,
                        tags=[" ".join(tag.split()) for tag in draft.tags if tag.strip()])
    try:
        if existing is None:
            day = _field(errors, "date", lambda: parse_date(draft.date)) if draft.date.strip() else None
            if errors:
                raise FormErrors(errors)
            return Task(kind=TaskKind.TODO, estimated_duration_minutes=TODO_PLACEHOLDER_MINUTES,
                        preferred_dates=[day] if day is not None else [], **fields)
        data = existing.model_dump()
        data.update(fields)
        return Task.model_validate(data)
    except ValidationError as error:
        raise FormErrors({"form": _first_message(error)}) from None


def _recurrence(draft: TaskDraft, errors: dict[str, str], start: date_ | None, timezone_name: str,
                existing: Task | None) -> RecurrenceSpec | None:
    """The draft's repeat fields as a configured rule starting on the form's date (errors collected)."""
    if draft.repeat not in REPEATS:
        errors["repeat"] = "Choose how the task repeats."
        return None
    if start is None:
        errors.setdefault("date", "A repeating task starts on a date: choose the day it starts.")
    interval = _field(errors, "repeat_interval", lambda: _positive(draft.repeat_interval or "1", "Every"))
    day_of_month = None
    if draft.repeat == "monthly" and draft.repeat_day_of_month.strip():
        day_of_month = _field(errors, "repeat_day_of_month", lambda: _positive(draft.repeat_day_of_month, "Day of month"))
        if day_of_month is not None and day_of_month > 31:
            errors["repeat_day_of_month"] = "Day of month is 1 to 31 (a month without it is skipped)."
    end_date = count = None
    if draft.repeat_end == "on":
        if not draft.repeat_until.strip():
            errors["repeat_until"] = "Choose the last date it can repeat on."
        else:
            end_date = _field(errors, "repeat_until", lambda: parse_date(draft.repeat_until))
    elif draft.repeat_end == "after":
        count = _field(errors, "repeat_count", lambda: _positive(draft.repeat_count, "Occurrences"))
    elif draft.repeat_end != "never":
        errors["repeat_end"] = "Choose when it stops repeating."
    if errors or start is None:
        return None
    tz_name = (existing.recurrence.timezone if existing is not None and existing.recurrence is not None
               and existing.recurrence.configured else timezone_name)
    try:
        return RecurrenceSpec(
            frequency=draft.repeat, interval=interval,
            weekdays=sorted(set(draft.repeat_weekdays)) or None if draft.repeat == "weekly" else None,
            day_of_month=day_of_month, end_date=end_date, count=count, start_date=start, timezone=tz_name,
        )
    except ValidationError as error:
        errors["repeat"] = _first_message(error)
        return None


def _positive(text: str, label: str) -> int:
    raw = (text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise FieldError(f"{label} must be a whole number of at least 1.")
    return int(raw)


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
    points = _field(errors, "points", lambda: parse_points(draft.points))
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
    fields = dict(label=label, category=draft.category.strip(), points=points, planned_date=day, timezone=tz_name,
                  planned_start=instants[0], planned_end=instants[1])
    try:
        return existing.model_copy(update=fields) if existing is not None else FixedBlock(**fields)
    except ValidationError as error:
        raise FormErrors({"form": _first_message(error)}) from None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def parse_points(text: str) -> int:
    """A task's points as typed: a whole number from 0 to 1000 (empty: the default)."""
    raw = (text or "").strip()
    if not raw:
        return DEFAULT_TASK_POINTS
    if not raw.isdigit():
        raise FieldError(f"Points are a whole number from 0 to {MAX_TASK_POINTS}, like {DEFAULT_TASK_POINTS} or 5.")
    value = int(raw)
    if value > MAX_TASK_POINTS:
        raise FieldError(f"Points go up to {MAX_TASK_POINTS}.")
    return value


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
    #: The workspace's reusable task types (independent of category and tags).
    task_types: list[Choice] = field(default_factory=list)
