"""
backend/record_mapping.py

The one field <-> column mapping of the record content that is stored
relationally (backend/models.py): a task's tags, preferred dates,
dependencies, recurrence and recurrence identity (docs/recurrence.md), and a
preference layer's overrides. The same
functions write and read a live row (tasks, preferences) and a revision row
(task_revisions, preference_revisions) -- they share their content columns
and child-relationship names -- so a record and its historical snapshots
can never be mapped differently.

Exactness:
    - tags, preferred dates and dependency ids are ordered child rows: a
      repeated value is stored as given, never de-duplicated;
    - recurrence weekdays are a set (RecurrenceSpec keeps them sorted and
      unique); no rows = weekdays None;
    - a preference category key is a row whose NULL value is an explicit
      clear (absent key = no row); reward.tag_relations is absent (flag
      false), present-and-empty (flag true, no rows), or a mapping whose
      related-tag lists may themselves be empty. Keys are read back in
      sorted order (a mapping is unordered, as in the JSON wire form).

Also the bound of the only JSON column, a placement's optimization_metadata.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import inspect

from app.planning.preferences import PreferenceOverrides
from backend.models import REWARD_FLOAT_FIELDS, REWARD_INT_FIELDS

#: optimization_metadata is a small, genuinely unstructured extension object that the wire contract and
#: the desktop allow on a placement (the optimizer itself writes none). Its compact JSON must fit this bound.
MAX_OPTIMIZATION_METADATA_BYTES = 4096
#: Top-level keys that would mean a schedule or task list is being stored in it instead of in real rows.
RESERVED_METADATA_KEYS = frozenset({
    "tasks", "task_ids", "placements", "scheduled_tasks", "schedule", "schedules", "fixed_blocks", "unscheduled",
})

REWARD_FIELDS = (*REWARD_FLOAT_FIELDS, *REWARD_INT_FIELDS)


def check_optimization_metadata(value: object) -> None:
    """Raise ValueError unless `value` is a JSON object within the extension bound."""
    if not isinstance(value, dict):
        raise ValueError("optimization_metadata must be a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("optimization_metadata must be a JSON object") from None
    if len(encoded.encode("utf-8")) > MAX_OPTIMIZATION_METADATA_BYTES:
        raise ValueError(f"optimization_metadata may be at most {MAX_OPTIMIZATION_METADATA_BYTES} bytes of JSON")
    reserved = sorted(RESERVED_METADATA_KEYS.intersection(value))
    if reserved:
        raise ValueError(
            "optimization_metadata is a small extension object and cannot hold a schedule or task list "
            f"(reserved keys: {', '.join(reserved)})"
        )


def _child(owner, relationship: str) -> type:
    """The mapped class of a row's (or row class's) child relationship."""
    return inspect(owner if isinstance(owner, type) else type(owner)).relationships[relationship].mapper.class_


# -----------------------------------------------------------------------------
# Tasks
# -----------------------------------------------------------------------------


def write_task(row, task) -> None:
    """Write a task's content (TaskFields/TaskOut/Task) into a tasks or task_revisions row and its child rows."""
    window = task.preferred_time_window
    recurrence = task.recurrence
    row.project_id = task.project_id
    row.name, row.category = task.name, task.category
    row.estimated_duration_minutes, row.priority, row.required = (
        task.estimated_duration_minutes, task.priority, task.required,
    )
    row.points = task.points
    row.task_type_id = task.task_type_id
    row.required_date = task.required_date
    row.preferred_window_start_minute = window.start_minute if window else None
    row.preferred_window_end_minute = window.end_minute if window else None
    row.deadline = task.deadline.isoformat() if task.deadline else None
    row.deadline_utc = task.deadline if task.deadline else None
    row.recurrence_frequency = recurrence.frequency.value if recurrence else None
    row.recurrence_interval = recurrence.interval if recurrence else None
    row.recurrence_day_of_month = recurrence.day_of_month if recurrence else None
    row.recurrence_end_date = recurrence.end_date if recurrence else None
    row.recurrence_count = recurrence.count if recurrence else None
    row.recurrence_start_date = recurrence.start_date if recurrence else None
    row.recurrence_timezone = recurrence.timezone if recurrence else None
    row.series_id, row.occurrence_slot = task.series_id, task.occurrence_slot
    state = task.occurrence_state
    row.occurrence_state = state.value if state is not None and not isinstance(state, str) else state
    row.series_version, row.series_predecessor_id = task.series_version, task.series_predecessor_id

    tag, preferred, dependency, weekday = (
        _child(row, name) for name in ("tag_rows", "preferred_date_rows", "dependency_rows", "recurrence_weekday_rows")
    )
    row.tag_rows = [tag(position=position, tag=value) for position, value in enumerate(task.tags)]
    row.preferred_date_rows = [
        preferred(position=position, preferred_date=day) for position, day in enumerate(task.preferred_dates)
    ]
    row.dependency_rows = [
        dependency(position=position, depends_on_id=task_id) for position, task_id in enumerate(task.dependency_ids)
    ]
    row.recurrence_weekday_rows = [weekday(weekday=day) for day in ((recurrence.weekdays or []) if recurrence else [])]


def task_content(row) -> dict:
    """A tasks/task_revisions row's content as TaskFields values (dependency ids included)."""
    window = None
    if row.preferred_window_start_minute is not None:
        window = {"start_minute": row.preferred_window_start_minute, "end_minute": row.preferred_window_end_minute}
    recurrence = None
    if row.recurrence_frequency is not None:
        weekdays = [item.weekday for item in row.recurrence_weekday_rows]
        recurrence = {
            "frequency": row.recurrence_frequency, "interval": row.recurrence_interval, "weekdays": weekdays or None,
            "day_of_month": row.recurrence_day_of_month, "end_date": row.recurrence_end_date,
            "count": row.recurrence_count, "start_date": row.recurrence_start_date,
            "timezone": row.recurrence_timezone,
        }
    return {
        "project_id": row.project_id, "name": row.name, "category": row.category,
        "tags": [item.tag for item in row.tag_rows],
        "estimated_duration_minutes": row.estimated_duration_minutes, "priority": row.priority,
        "points": row.points,
        "task_type_id": row.task_type_id,
        "required": row.required, "required_date": row.required_date,
        "preferred_dates": [item.preferred_date for item in row.preferred_date_rows],
        "preferred_time_window": window,
        "dependency_ids": [item.depends_on_id for item in row.dependency_rows],
        "deadline": datetime.fromisoformat(row.deadline) if row.deadline else None,
        "recurrence": recurrence,
        "series_id": row.series_id, "occurrence_slot": row.occurrence_slot,
        "occurrence_state": row.occurrence_state, "series_version": row.series_version,
        "series_predecessor_id": row.series_predecessor_id,
    }


# -----------------------------------------------------------------------------
# Placement planning snapshots
# -----------------------------------------------------------------------------

#: The scalar snapshot columns of a placements/placement_revisions row (task_tags are child rows).
PLACEMENT_SNAPSHOT_COLUMNS = ("task_category", "task_name", "task_points", "task_estimate_minutes", "task_type_id",
                              "task_type_label")


def write_placement_snapshot(row, placement, *, keep_recorded: bool) -> None:
    """
    Write a placement's planning snapshot (PlacementFields/PlacementOut) into a
    placements or placement_revisions row. A snapshot is history: a value the
    payload does not carry (None) never clears a recorded one, and with
    keep_recorded (an update of a stored row) a recorded value is never
    replaced -- only a fact that was unknown is filled in.
    """
    for name in PLACEMENT_SNAPSHOT_COLUMNS:
        value = getattr(placement, name)
        if value is not None and not (keep_recorded and getattr(row, name) is not None):
            setattr(row, name, value)
    if placement.task_tags is not None and not (keep_recorded and row.task_tags_recorded):
        tag = _child(row, "task_tag_rows")
        row.task_tags_recorded = True
        row.task_tag_rows = [tag(position=position, tag=value) for position, value in enumerate(placement.task_tags)]
    elif row.task_tags_recorded is None:
        row.task_tags_recorded = False


def placement_snapshot(row) -> dict:
    """A placements/placement_revisions row's planning snapshot as PlacementFields values."""
    return {
        **{name: getattr(row, name) for name in PLACEMENT_SNAPSHOT_COLUMNS},
        "task_tags": [item.tag for item in row.task_tag_rows] if row.task_tags_recorded else None,
    }


# -----------------------------------------------------------------------------
# Preference layers
# -----------------------------------------------------------------------------


def write_preference_overrides(row, overrides: PreferenceOverrides) -> None:
    """Write one PreferenceOverrides layer into a preferences or preference_revisions row and its child rows."""
    mode = overrides.optimizer_mode
    window = overrides.day_window
    reward = overrides.reward
    row.optimizer_mode = mode.value if mode is not None else None
    row.day_window_start_minute = window.start_minute if window else None
    row.day_window_end_minute = window.end_minute if window else None
    row.day_window_end_day_offset = window.end_day_offset if window else None
    for name in REWARD_FIELDS:
        setattr(row, f"reward_{name}", getattr(reward, name))
    row.reward_tag_relations_present = reward.tag_relations is not None

    multiplier, category_window, relation = (
        _child(row, name) for name in ("category_multiplier_rows", "category_window_rows", "tag_relation_rows")
    )
    related = _child(relation, "related_rows")
    row.category_multiplier_rows = [
        multiplier(category=category, multiplier=value)
        for category, value in sorted(overrides.category_multipliers.items())
    ]
    row.category_window_rows = [
        category_window(category=category, start_minute=value.start_minute if value else None,
                        end_minute=value.end_minute if value else None)
        for category, value in sorted(overrides.category_preferred_windows.items())
    ]
    row.tag_relation_rows = [
        relation(tag=tag, related_rows=[related(position=position, related_tag=value)
                                         for position, value in enumerate(values)])
        for tag, values in sorted((reward.tag_relations or {}).items())
    ]


def preference_overrides(row) -> PreferenceOverrides:
    """The PreferenceOverrides layer of a preferences/preference_revisions row (optimizer_mode included)."""
    window = None
    if row.day_window_start_minute is not None:
        window = {"start_minute": row.day_window_start_minute, "end_minute": row.day_window_end_minute,
                  "end_day_offset": row.day_window_end_day_offset}
    reward = {name: getattr(row, f"reward_{name}") for name in REWARD_FIELDS}
    reward["tag_relations"] = (
        {item.tag: [value.related_tag for value in item.related_rows] for item in row.tag_relation_rows}
        if row.reward_tag_relations_present else None
    )
    return PreferenceOverrides.model_validate({
        "day_window": window,
        "category_multipliers": {item.category: item.multiplier for item in row.category_multiplier_rows},
        "category_preferred_windows": {
            item.category: None if item.start_minute is None
            else {"start_minute": item.start_minute, "end_minute": item.end_minute}
            for item in row.category_window_rows
        },
        "optimizer_mode": row.optimizer_mode,
        "reward": reward,
    })
