"""
backend/migrations/normalized_storage.py

Frozen conversion code of the normalized-storage migrations: 0005 (backfill)
and 0006 (validate and contract). It converts the structured JSON the
revision-0003 schema stored --

    tasks.tags / preferred_dates / recurrence       -> child rows + recurrence scalars
    preferences.overrides                           -> scalar columns + category/tag-relation rows
    change_log.payload (a whole API record)         -> an immutable typed record revision
    sync_operations.result (a whole push result)    -> typed outcome columns + revisions + problem rows

-- and back (for 0006's validation, which rebuilds every document from the
rows actually stored and compares it with the original, and for its
downgrade). It deliberately imports nothing from the application: its
meaning is fixed at the time of these migrations and must never change.

Everything is strict. A value that is not exactly the shape the 0003-era
server wrote fails the upgrade with a MigrationDataError naming the table,
the row's key and the field -- never the value, which may be private -- and
the whole upgrade rolls back. Nothing is dropped, rounded, de-duplicated or
guessed.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from backend.database import UTCDateTime

BATCH_SIZE = 500
MAX_METADATA_BYTES = 4096
RESERVED_METADATA_KEYS = frozenset({
    "tasks", "task_ids", "placements", "scheduled_tasks", "schedule", "schedules", "fixed_blocks", "unscheduled",
})
REWARD_FLOAT = (
    "weight_importance", "weight_time_bonus", "weight_tag_relation", "weight_fragmentation_penalty",
    "weight_category_bonus", "short_gap_bonus_weight", "short_gap_bonus_cap",
)
REWARD_INT = ("max_time_distance_minutes", "same_tag_window_minutes", "min_gap_between_tasks_minutes",
              "short_gap_bonus_max_minutes")
#: RewardPreferencesOverride's field order (model_dump order).
REWARD_ORDER = (
    "weight_importance", "weight_time_bonus", "weight_tag_relation", "weight_fragmentation_penalty",
    "weight_category_bonus", "max_time_distance_minutes", "same_tag_window_minutes", "min_gap_between_tasks_minutes",
    "short_gap_bonus_weight", "short_gap_bonus_max_minutes", "short_gap_bonus_cap",
)
NAMESPACE = uuid.UUID("5d0c7c2e-3b1f-4f7e-9b52-0d9f3c6a1e04")

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


class MigrationDataError(RuntimeError):
    """Existing data cannot be converted exactly. The message names rows and fields, never values."""


def fail(where: str, problem: str) -> None:
    raise MigrationDataError(
        f"{where}: {problem}. The upgrade was rolled back and nothing was changed; correct or remove this "
        "record (for example with the previous application version) and run the upgrade again."
    )


# -----------------------------------------------------------------------------
# Tables (only the columns these migrations read or write)
# -----------------------------------------------------------------------------

_md = sa.MetaData()
UUID_, INT, FLOAT, BOOL, TEXT, DATE, INSTANT = (
    sa.Uuid(), sa.Integer(), sa.Float(), sa.Boolean(), sa.Text(), sa.Date(), UTCDateTime()
)


def _table(name: str, *columns: tuple[str, object]) -> sa.Table:
    return sa.Table(name, _md, *(sa.Column(column, kind) for column, kind in columns))


TASK_CONTENT = (
    ("project_id", UUID_), ("name", TEXT), ("category", TEXT), ("estimated_duration_minutes", INT), ("priority", INT),
    ("required", BOOL), ("required_date", DATE), ("preferred_window_start_minute", INT), ("preferred_window_end_minute", INT),
    ("deadline", TEXT), ("deadline_utc", INSTANT),
)
RECURRENCE = (("recurrence_frequency", TEXT), ("recurrence_interval", INT), ("recurrence_day_of_month", INT),
              ("recurrence_end_date", DATE), ("recurrence_count", INT))
PREFERENCE_SCALARS = (
    ("optimizer_mode", TEXT), ("day_window_start_minute", INT), ("day_window_end_minute", INT),
    ("day_window_end_day_offset", INT), *((f"reward_{name}", FLOAT) for name in REWARD_FLOAT),
    *((f"reward_{name}", INT) for name in REWARD_INT), ("reward_tag_relations_present", BOOL),
)
EXECUTION_CONTENT = (
    ("legacy_id", TEXT), ("task_id", UUID_), ("scheduled_task_id", UUID_), ("historical_reference", BOOL), ("task_name", TEXT),
    ("category", TEXT), ("tag", TEXT), ("planned_date", INT), ("planned_start", INT), ("planned_end", INT),
    ("planned_duration", INT), ("priority", INT), ("status", TEXT), ("actual_active_duration_minutes", FLOAT),
    ("duration_variance_minutes", FLOAT), ("start_delay_minutes", FLOAT), ("focus_rating", INT), ("energy_rating", INT),
    ("interruption_count", INT), ("note", TEXT), ("canonical_planned_date", DATE), ("canonical_timezone", TEXT),
    ("canonical_planned_start", INSTANT), ("canonical_planned_end", INSTANT), ("actual_first_start_at", INSTANT),
    ("actual_final_end_at", INSTANT),
)
GENERATION_CONTENT = (
    ("planned_date", DATE), ("timezone", TEXT), ("engine_mode", TEXT), ("range_start", DATE), ("range_end", DATE),
    ("range_scope", TEXT), ("allocation_id", UUID_), ("fingerprint", TEXT), ("fingerprint_version", INT),
    ("placements_digest", TEXT), ("placement_count", INT), ("unscheduled_count", INT), ("total_score", FLOAT),
    ("generated_at", INSTANT),
)
BLOCK_CONTENT = (("label", TEXT), ("category", TEXT), ("planned_date", DATE), ("timezone", TEXT),
                 ("planned_start", INSTANT), ("planned_end", INSTANT))
PLACEMENT_CONTENT = (("task_id", UUID_), ("planned_date", DATE), ("timezone", TEXT), ("planned_start", INSTANT),
                     ("planned_end", INSTANT), ("score", FLOAT), ("optimization_metadata", JSON_DOCUMENT))

tasks = _table("tasks", ("user_id", UUID_), ("id", UUID_), ("tags", JSON_DOCUMENT), ("preferred_dates", JSON_DOCUMENT),
               ("recurrence", JSON_DOCUMENT), *RECURRENCE)
task_tags = _table("task_tags", ("user_id", UUID_), ("task_id", UUID_), ("position", INT), ("tag", TEXT))
task_preferred_dates = _table("task_preferred_dates", ("user_id", UUID_), ("task_id", UUID_), ("position", INT),
                              ("preferred_date", DATE))
task_recurrence_weekdays = _table("task_recurrence_weekdays", ("user_id", UUID_), ("task_id", UUID_), ("weekday", INT))
preferences = _table("preferences", ("user_id", UUID_), ("id", UUID_), ("overrides", JSON_DOCUMENT), *PREFERENCE_SCALARS)
preference_category_multipliers = _table("preference_category_multipliers", ("user_id", UUID_), ("preference_id", UUID_),
                                         ("category", TEXT), ("multiplier", FLOAT))
preference_category_windows = _table("preference_category_windows", ("user_id", UUID_), ("preference_id", UUID_),
                                     ("category", TEXT), ("start_minute", INT), ("end_minute", INT))
preference_tag_relations = _table("preference_tag_relations", ("user_id", UUID_), ("preference_id", UUID_), ("tag", TEXT))
preference_related_tags = _table("preference_related_tags", ("user_id", UUID_), ("preference_id", UUID_), ("tag", TEXT),
                                 ("position", INT), ("related_tag", TEXT))
placements = _table("placements", ("user_id", UUID_), ("id", UUID_), ("optimization_metadata", JSON_DOCUMENT))
executions = _table("executions", ("user_id", UUID_), ("id", UUID_), ("task_id", UUID_), ("scheduled_task_id", UUID_),
                    ("historical_reference", BOOL), ("linked_task_id", UUID_), ("linked_placement_id", UUID_))
change_log = _table("change_log", ("user_id", UUID_), ("seq", sa.BigInteger()), ("entity_type", TEXT), ("entity_id", UUID_),
                    ("version", INT), ("payload", JSON_DOCUMENT), ("revision_id", UUID_))
sync_operations = _table(
    "sync_operations", ("user_id", UUID_), ("op_id", UUID_), ("status", TEXT), ("result", JSON_DOCUMENT),
    ("record_revision_id", UUID_), ("error_code", TEXT), ("error_message", TEXT), ("error_supplied_version", INT),
    ("error_current_version", INT), ("error_current_revision_id", UUID_), ("error_conflicting_revision_id", UUID_),
    ("error_reason", TEXT), ("error_failed_op_id", UUID_), ("error_problems_present", BOOL),
)
sync_operation_problems = _table("sync_operation_problems", ("user_id", UUID_), ("op_id", UUID_), ("position", INT),
                                 ("message", TEXT))
sync_operation_problem_locations = _table("sync_operation_problem_locations", ("user_id", UUID_), ("op_id", UUID_),
                                          ("problem_position", INT), ("position", INT), ("part", TEXT))
record_revisions = _table("record_revisions", ("user_id", UUID_), ("id", UUID_), ("entity_type", TEXT), ("entity_id", UUID_),
                          ("version", INT), ("created_at", INSTANT), ("updated_at", INSTANT), ("deleted_at", INSTANT))
DETAIL = {
    "project": _table("project_revisions", ("user_id", UUID_), ("id", UUID_), ("name", TEXT), ("description", TEXT)),
    "task": _table("task_revisions", ("user_id", UUID_), ("id", UUID_), *TASK_CONTENT, *RECURRENCE),
    "fixed_block": _table("fixed_block_revisions", ("user_id", UUID_), ("id", UUID_), *BLOCK_CONTENT),
    "placement": _table("placement_revisions", ("user_id", UUID_), ("id", UUID_), *PLACEMENT_CONTENT),
    "preference": _table("preference_revisions", ("user_id", UUID_), ("id", UUID_), ("scope", TEXT), ("scope_date", DATE),
                         ("scope_key", TEXT), *PREFERENCE_SCALARS),
    "schedule_generation": _table("schedule_generation_revisions", ("user_id", UUID_), ("id", UUID_), *GENERATION_CONTENT),
    "execution": _table("execution_revisions", ("user_id", UUID_), ("id", UUID_), *EXECUTION_CONTENT),
}
REVISION_CHILDREN = {
    "task_revision_tags": _table("task_revision_tags", ("user_id", UUID_), ("revision_id", UUID_), ("position", INT),
                                 ("tag", TEXT)),
    "task_revision_preferred_dates": _table("task_revision_preferred_dates", ("user_id", UUID_), ("revision_id", UUID_),
                                            ("position", INT), ("preferred_date", DATE)),
    "task_revision_dependencies": _table("task_revision_dependencies", ("user_id", UUID_), ("revision_id", UUID_),
                                         ("position", INT), ("depends_on_id", UUID_)),
    "task_revision_recurrence_weekdays": _table("task_revision_recurrence_weekdays", ("user_id", UUID_),
                                                ("revision_id", UUID_), ("weekday", INT)),
    "preference_revision_category_multipliers": _table(
        "preference_revision_category_multipliers", ("user_id", UUID_), ("revision_id", UUID_), ("category", TEXT),
        ("multiplier", FLOAT)),
    "preference_revision_category_windows": _table(
        "preference_revision_category_windows", ("user_id", UUID_), ("revision_id", UUID_), ("category", TEXT),
        ("start_minute", INT), ("end_minute", INT)),
    "preference_revision_tag_relations": _table("preference_revision_tag_relations", ("user_id", UUID_),
                                                ("revision_id", UUID_), ("tag", TEXT)),
    "preference_revision_related_tags": _table("preference_revision_related_tags", ("user_id", UUID_),
                                               ("revision_id", UUID_), ("tag", TEXT), ("position", INT), ("related_tag", TEXT)),
    "execution_revision_sessions": _table("execution_revision_sessions", ("user_id", UUID_), ("revision_id", UUID_),
                                          ("position", INT), ("started_at", INSTANT), ("ended_at", INSTANT)),
}
#: Every table a backfill writes, parents first (the insert order) -- deleted in reverse by the 0005 downgrade.
WRITTEN_TABLES = (
    task_tags, task_preferred_dates, task_recurrence_weekdays, preference_category_multipliers,
    preference_category_windows, preference_tag_relations, preference_related_tags, record_revisions,
    *DETAIL.values(), *REVISION_CHILDREN.values(), sync_operation_problems, sync_operation_problem_locations,
)


# -----------------------------------------------------------------------------
# Strict value parsing (JSON -> Python) and formatting (Python -> JSON)
# -----------------------------------------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse(kind: str, value, where: str, field: str):
    """One JSON value of `kind` ("str", "int", "float", "bool", "uuid", "date", "instant"; "?" = nullable)."""
    if kind.endswith("?"):
        if value is None:
            return None
        kind = kind[:-1]
    if kind == "str" and isinstance(value, str):
        return value
    if kind == "int" and _is_int(value):
        return value
    if kind == "float" and (_is_int(value) or isinstance(value, float)) and math.isfinite(value):
        return float(value)
    if kind == "bool" and isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            if kind == "uuid":
                return uuid.UUID(value)
            if kind == "date" and len(value) == 10:
                return date.fromisoformat(value)
            if kind == "instant":
                parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    fail(where, f"field {field!r} is not a valid {kind} value")


def instant_text(value: datetime) -> str:
    """An instant as the API writes it (pydantic: UTC with a trailing Z)."""
    text = value.astimezone(timezone.utc).isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def fmt(kind: str, value):
    """The JSON form of a stored value of `kind`."""
    if value is None:
        return None
    kind = kind.rstrip("?")
    if kind == "uuid":
        return str(value)
    if kind == "date":
        return value.isoformat()
    if kind == "instant":
        return instant_text(value)
    if kind == "float":
        return float(value)
    return value


def exact_keys(value, keys, where: str, what: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(keys):
        fail(where, f"{what} does not have exactly the expected fields")
    return value


def string_list(value, where: str, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail(where, f"field {field!r} is not a list of strings")
    return value


def check_metadata(value, where: str) -> None:
    if not isinstance(value, dict):
        fail(where, "optimization_metadata is not a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        fail(where, "optimization_metadata is not valid JSON")
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        fail(where, f"optimization_metadata is larger than the {MAX_METADATA_BYTES}-byte extension bound")
    if RESERVED_METADATA_KEYS.intersection(value):
        fail(where, "optimization_metadata holds a schedule or task list (a reserved top-level key)")


def as_instant(text: str) -> datetime | None:
    if "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def same(a, b) -> bool:
    """
    Semantic JSON equality: the same keys (a null value is not an absent key),
    lists in the same order, numbers by value, and two strings that are the
    same instant (the stored form of an instant is UTC) are equal.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str) and a != b:
        first, second = as_instant(a), as_instant(b)
        return first is not None and first == second
    return a == b


# -----------------------------------------------------------------------------
# Task content (tags, preferred dates, dependencies, recurrence)
# -----------------------------------------------------------------------------

RECURRENCE_KEYS = ("frequency", "interval", "weekdays", "day_of_month", "end_date", "count")


def recurrence_columns(value, where: str) -> tuple[dict, list[int]]:
    """RecurrenceSpec JSON -> (recurrence_* columns, weekdays)."""
    if value is None:
        return {name: None for name, _ in RECURRENCE}, []
    if not isinstance(value, dict) or not set(value) <= set(RECURRENCE_KEYS) or "frequency" not in value:
        fail(where, "field 'recurrence' is not a recurrence rule")
    frequency = value["frequency"]
    interval = parse("int", value.get("interval", 1), where, "recurrence.interval")
    day_of_month = parse("int?", value.get("day_of_month"), where, "recurrence.day_of_month")
    end_date = parse("date?", value.get("end_date"), where, "recurrence.end_date")
    count = parse("int?", value.get("count"), where, "recurrence.count")
    weekdays = value.get("weekdays")
    if frequency not in ("daily", "weekly", "monthly") or interval <= 0 or (count is not None and count <= 0):
        fail(where, "field 'recurrence' has an invalid frequency, interval or count")
    if (end_date is not None and count is not None) or (day_of_month is not None and frequency != "monthly"):
        fail(where, "field 'recurrence' combines selectors the recurrence rule does not allow")
    if day_of_month is not None and not 1 <= day_of_month <= 31:
        fail(where, "field 'recurrence.day_of_month' is out of range")
    if weekdays is not None:
        if (frequency != "weekly" or not isinstance(weekdays, list) or not weekdays
                or not all(_is_int(day) and 0 <= day <= 6 for day in weekdays) or weekdays != sorted(set(weekdays))):
            fail(where, "field 'recurrence.weekdays' is not a sorted set of weekdays of a weekly rule")
    columns = {"recurrence_frequency": frequency, "recurrence_interval": interval,
               "recurrence_day_of_month": day_of_month, "recurrence_end_date": end_date, "recurrence_count": count}
    return columns, list(weekdays or [])


def recurrence_json(columns, weekdays: list[int]):
    if columns["recurrence_frequency"] is None:
        return None
    return {
        "frequency": columns["recurrence_frequency"], "interval": columns["recurrence_interval"],
        "weekdays": list(weekdays) or None, "day_of_month": columns["recurrence_day_of_month"],
        "end_date": fmt("date", columns["recurrence_end_date"]), "count": columns["recurrence_count"],
    }


# -----------------------------------------------------------------------------
# Preference overrides
# -----------------------------------------------------------------------------

OVERRIDE_KEYS = ("day_window", "category_multipliers", "category_preferred_windows", "reward")


def overrides_rows(document, where: str, *, with_mode: bool, mode=None) -> tuple[dict, list, list, list]:
    """
    A PreferenceOverrides document -> (scalar columns, multipliers, windows,
    tag relations). The live preferences.overrides document has no
    optimizer_mode (a column of its own: `mode`); a record's overrides does.
    Absent keys mean what PreferenceOverrides' defaults mean.
    """
    allowed = (*OVERRIDE_KEYS, "optimizer_mode") if with_mode else OVERRIDE_KEYS
    if not isinstance(document, dict) or not set(document) <= set(allowed):
        fail(where, "the preference overrides are not a preference layer")
    if with_mode:
        mode = document.get("optimizer_mode")
    if mode not in (None, "precise_greedy", "adhd_friendly"):
        fail(where, "the preference layer's optimizer_mode is not a known mode")
    columns: dict = {"optimizer_mode": mode}

    window = document.get("day_window")
    if window is not None:
        window = exact_keys(window, ("start_minute", "end_minute", "end_day_offset"), where, "day_window")
    for key in ("start_minute", "end_minute", "end_day_offset"):
        columns[f"day_window_{key}"] = None if window is None else parse("int", window[key], where, f"day_window.{key}")

    multipliers = document.get("category_multipliers", {})
    windows = document.get("category_preferred_windows", {})
    if not isinstance(multipliers, dict) or not isinstance(windows, dict):
        fail(where, "the category overrides are not mappings")
    multiplier_rows = [{"category": category, "multiplier": parse("float?", value, where, "category_multipliers")}
                       for category, value in sorted(multipliers.items())]
    window_rows = []
    for category, value in sorted(windows.items()):
        if value is not None:
            value = exact_keys(value, ("start_minute", "end_minute"), where, "a category window")
        window_rows.append({
            "category": category,
            "start_minute": None if value is None else parse("int", value["start_minute"], where, "category window"),
            "end_minute": None if value is None else parse("int", value["end_minute"], where, "category window"),
        })

    reward = document.get("reward", {})
    if not isinstance(reward, dict) or not set(reward) <= {*REWARD_ORDER, "tag_relations"}:
        fail(where, "the reward overrides are not a reward layer")
    for name in REWARD_FLOAT:
        columns[f"reward_{name}"] = parse("float?", reward.get(name), where, f"reward.{name}")
    for name in REWARD_INT:
        columns[f"reward_{name}"] = parse("int?", reward.get(name), where, f"reward.{name}")
    relations = reward.get("tag_relations")
    columns["reward_tag_relations_present"] = relations is not None
    relation_rows = []
    if relations is not None:
        if not isinstance(relations, dict):
            fail(where, "reward.tag_relations is not a mapping")
        relation_rows = [(tag, string_list(related, where, "reward.tag_relations")) for tag, related in
                         sorted(relations.items())]
    return columns, multiplier_rows, window_rows, relation_rows


def overrides_json(columns, multipliers, windows, relations, *, with_mode: bool) -> dict:
    """The PreferenceOverrides document of stored rows, with every key (model_dump form)."""
    window = None
    if columns["day_window_start_minute"] is not None:
        window = {"start_minute": columns["day_window_start_minute"], "end_minute": columns["day_window_end_minute"],
                  "end_day_offset": columns["day_window_end_day_offset"]}
    reward = {name: columns[f"reward_{name}"] for name in REWARD_ORDER}
    reward["tag_relations"] = dict(relations) if columns["reward_tag_relations_present"] else None
    document = {
        "day_window": window,
        "category_multipliers": {row["category"]: row["multiplier"] for row in multipliers},
        "category_preferred_windows": {
            row["category"]: None if row["start_minute"] is None
            else {"start_minute": row["start_minute"], "end_minute": row["end_minute"]}
            for row in windows
        },
    }
    if with_mode:
        document["optimizer_mode"] = columns["optimizer_mode"]
    document["reward"] = reward
    return document


def normalized_overrides(document, *, with_mode: bool) -> dict:
    """A stored overrides document with every absent key filled in as PreferenceOverrides' default."""
    reward = {name: None for name in REWARD_ORDER}
    reward["tag_relations"] = None
    reward.update(document.get("reward", {}))
    result = {"day_window": document.get("day_window"),
              "category_multipliers": document.get("category_multipliers", {}),
              "category_preferred_windows": document.get("category_preferred_windows", {})}
    if with_mode:
        result["optimizer_mode"] = document.get("optimizer_mode")
    result["reward"] = reward
    return result


# -----------------------------------------------------------------------------
# API records <-> typed revisions
# -----------------------------------------------------------------------------

META = (("id", "uuid"), ("version", "int"), ("created_at", "instant"), ("updated_at", "instant"),
        ("deleted_at", "instant?"))
SCALARS = {
    "project": (("name", "str"), ("description", "str?")),
    "fixed_block": (("label", "str"), ("category", "str"), ("planned_date", "date"), ("timezone", "str"),
                    ("planned_start", "instant"), ("planned_end", "instant")),
    "placement": (("task_id", "uuid"), ("planned_date", "date"), ("timezone", "str"), ("planned_start", "instant"),
                  ("planned_end", "instant"), ("score", "float")),
    "schedule_generation": (
        ("planned_date", "date"), ("timezone", "str"), ("engine_mode", "str"), ("range_start", "date"),
        ("range_end", "date"), ("range_scope", "str"), ("allocation_id", "uuid"), ("fingerprint", "str"),
        ("fingerprint_version", "int"), ("placements_digest", "str"), ("placement_count", "int"),
        ("unscheduled_count", "int"), ("total_score", "float"), ("generated_at", "instant")),
    "execution": (
        ("legacy_id", "str?"), ("task_id", "uuid?"), ("scheduled_task_id", "uuid?"), ("historical_reference", "bool"),
        ("task_name", "str"), ("category", "str"), ("tag", "str"), ("planned_date", "int?"), ("planned_start", "int?"),
        ("planned_end", "int?"), ("planned_duration", "int"), ("priority", "int"), ("status", "str"),
        ("actual_active_duration_minutes", "float?"), ("duration_variance_minutes", "float?"),
        ("start_delay_minutes", "float?"), ("focus_rating", "int?"), ("energy_rating", "int?"),
        ("interruption_count", "int?"), ("note", "str?"), ("canonical_planned_date", "date?"),
        ("canonical_timezone", "str?"), ("canonical_planned_start", "instant?"), ("canonical_planned_end", "instant?"),
        ("actual_first_start_at", "instant?"), ("actual_final_end_at", "instant?")),
    "task": (("project_id", "uuid?"), ("name", "str"), ("category", "str"), ("estimated_duration_minutes", "int"),
             ("priority", "int"), ("required", "bool"), ("required_date", "date?")),
    "preference": (),
}
#: Each type's non-scalar record fields, in the API's field order.
STRUCTURED = {
    "task": ("tags", "preferred_dates", "preferred_time_window", "dependency_ids", "deadline", "recurrence"),
    "placement": ("optimization_metadata",),
    "preference": ("scope", "date", "overrides"),
    "execution": ("sessions",),
}
#: The API's field order of each record type (RecordMeta last), for rebuilding a record.
FIELD_ORDER = {
    "project": ("name", "description"),
    "task": ("project_id", "name", "category", "tags", "estimated_duration_minutes", "priority", "required",
             "required_date", "preferred_dates", "preferred_time_window", "dependency_ids", "deadline", "recurrence"),
    "fixed_block": ("label", "category", "planned_date", "timezone", "planned_start", "planned_end"),
    "placement": ("task_id", "planned_date", "timezone", "planned_start", "planned_end", "score",
                  "optimization_metadata"),
    "preference": ("scope", "date", "overrides"),
    "schedule_generation": tuple(name for name, _ in SCALARS["schedule_generation"]),
    "execution": ("legacy_id", "task_id", "scheduled_task_id", "historical_reference", "task_name", "category",
                  "tag", "planned_date", "planned_start", "planned_end", "planned_duration", "priority", "status",
                  "sessions", "actual_active_duration_minutes", "duration_variance_minutes", "start_delay_minutes",
                  "focus_rating", "energy_rating", "interruption_count", "note", "canonical_planned_date",
                  "canonical_timezone", "canonical_planned_start", "canonical_planned_end", "actual_first_start_at",
                  "actual_final_end_at"),
}
TYPE_BY_KEYS = {frozenset((*fields, *(name for name, _ in META))): entity_type
                for entity_type, fields in FIELD_ORDER.items()}


def entity_type_of(record, where: str) -> str:
    entity_type = TYPE_BY_KEYS.get(frozenset(record)) if isinstance(record, dict) else None
    if entity_type is None:
        fail(where, "a stored record snapshot is not a record of a known type")
    return entity_type


def revision_id(*parts) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, "/".join(str(part) for part in parts))


def revision_rows(user_id: uuid.UUID, rid: uuid.UUID, entity_type: str, record, where: str) -> list[tuple]:
    """An API record -> [(table, row), ...] of its revision: the header, the typed row, its child rows."""
    exact_keys(record, {*FIELD_ORDER[entity_type], *(name for name, _ in META)}, where,
               f"the {entity_type} snapshot")
    header = {"user_id": user_id, "id": rid, "entity_type": entity_type}
    for name, kind in META:
        header["entity_id" if name == "id" else name] = parse(kind, record[name], where, name)
    detail = {"user_id": user_id, "id": rid}
    for name, kind in SCALARS[entity_type]:
        detail[name] = parse(kind, record[name], where, name)
    key = {"user_id": user_id, "revision_id": rid}
    rows: list[tuple] = [(record_revisions, header), (DETAIL[entity_type], detail)]

    if entity_type == "placement":
        check_metadata(record["optimization_metadata"], where)
        detail["optimization_metadata"] = record["optimization_metadata"]
    elif entity_type == "execution":
        if detail["status"] not in ("scheduled", "in_progress", "paused", "completed", "skipped", "cancelled"):
            fail(where, "field 'status' is not an execution status")
        sessions = record["sessions"]
        if not isinstance(sessions, list):
            fail(where, "field 'sessions' is not a list")
        for position, work in enumerate(sessions):
            work = exact_keys(work, ("started_at", "ended_at"), where, "a work session")
            rows.append((REVISION_CHILDREN["execution_revision_sessions"], {
                **key, "position": position, "started_at": parse("instant", work["started_at"], where, "sessions"),
                "ended_at": parse("instant?", work["ended_at"], where, "sessions"),
            }))
    elif entity_type == "task":
        window = record["preferred_time_window"]
        if window is not None:
            window = exact_keys(window, ("start_minute", "end_minute"), where, "preferred_time_window")
        detail["preferred_window_start_minute"] = None if window is None else parse(
            "int", window["start_minute"], where, "preferred_time_window")
        detail["preferred_window_end_minute"] = None if window is None else parse(
            "int", window["end_minute"], where, "preferred_time_window")
        deadline = parse("instant?", record["deadline"], where, "deadline")
        if deadline is not None:
            text = record["deadline"]
            original = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            detail["deadline"], detail["deadline_utc"] = original.isoformat(), deadline
        else:
            detail["deadline"] = detail["deadline_utc"] = None
        columns, weekdays = recurrence_columns(record["recurrence"], where)
        detail.update(columns)
        for position, tag in enumerate(string_list(record["tags"], where, "tags")):
            rows.append((REVISION_CHILDREN["task_revision_tags"], {**key, "position": position, "tag": tag}))
        if not isinstance(record["preferred_dates"], list) or not isinstance(record["dependency_ids"], list):
            fail(where, "fields 'preferred_dates'/'dependency_ids' are not lists")
        for position, day in enumerate(record["preferred_dates"]):
            rows.append((REVISION_CHILDREN["task_revision_preferred_dates"],
                         {**key, "position": position, "preferred_date": parse("date", day, where, "preferred_dates")}))
        for position, task_id in enumerate(record["dependency_ids"]):
            rows.append((REVISION_CHILDREN["task_revision_dependencies"],
                         {**key, "position": position, "depends_on_id": parse("uuid", task_id, where, "dependency_ids")}))
        for day in weekdays:
            rows.append((REVISION_CHILDREN["task_revision_recurrence_weekdays"], {**key, "weekday": day}))
    elif entity_type == "preference":
        scope = record["scope"]
        scope_date = parse("date?", record["date"], where, "date")
        if scope not in ("user", "date") or (scope == "date") != (scope_date is not None):
            fail(where, "fields 'scope'/'date' are not a preference scope")
        columns, multipliers, windows, relations = overrides_rows(record["overrides"], where, with_mode=True)
        detail.update(columns, scope=scope, scope_date=scope_date,
                      scope_key="user" if scope == "user" else scope_date.isoformat())
        for row in multipliers:
            rows.append((REVISION_CHILDREN["preference_revision_category_multipliers"], {**key, **row}))
        for row in windows:
            rows.append((REVISION_CHILDREN["preference_revision_category_windows"], {**key, **row}))
        for tag, related in relations:
            rows.append((REVISION_CHILDREN["preference_revision_tag_relations"], {**key, "tag": tag}))
            for position, value in enumerate(related):
                rows.append((REVISION_CHILDREN["preference_revision_related_tags"],
                             {**key, "tag": tag, "position": position, "related_tag": value}))
    return rows


def deadline_json(text: str | None):
    if text is None:
        return None
    value = datetime.fromisoformat(text)
    return instant_text(value) if value.utcoffset().total_seconds() == 0 else value.isoformat()


def revision_json(entity_type: str, header, detail, children: dict[str, list]) -> dict:
    """A stored revision -> its API record (the field order and forms the API writes)."""
    values = {name: fmt(kind, detail[name]) for name, kind in SCALARS[entity_type]}
    if entity_type == "placement":
        values["optimization_metadata"] = detail["optimization_metadata"]
    elif entity_type == "execution":
        values["sessions"] = [{"started_at": fmt("instant", row["started_at"]), "ended_at": fmt("instant?", row["ended_at"])}
                              for row in children.get("execution_revision_sessions", [])]
    elif entity_type == "task":
        window = None
        if detail["preferred_window_start_minute"] is not None:
            window = {"start_minute": detail["preferred_window_start_minute"],
                      "end_minute": detail["preferred_window_end_minute"]}
        values.update(
            tags=[row["tag"] for row in children.get("task_revision_tags", [])],
            preferred_dates=[fmt("date", row["preferred_date"])
                             for row in children.get("task_revision_preferred_dates", [])],
            preferred_time_window=window,
            dependency_ids=[str(row["depends_on_id"]) for row in children.get("task_revision_dependencies", [])],
            deadline=deadline_json(detail["deadline"]),
            recurrence=recurrence_json(detail, [row["weekday"] for row in
                                                children.get("task_revision_recurrence_weekdays", [])]),
        )
    elif entity_type == "preference":
        related = defaultdict(list)
        for row in children.get("preference_revision_related_tags", []):
            related[row["tag"]].append(row["related_tag"])
        relations = [(row["tag"], related[row["tag"]]) for row in children.get("preference_revision_tag_relations", [])]
        values.update(scope=detail["scope"], date=fmt("date?", detail["scope_date"]), overrides=overrides_json(
            detail, children.get("preference_revision_category_multipliers", []),
            children.get("preference_revision_category_windows", []), relations, with_mode=True))
    record = {name: values[name] for name in FIELD_ORDER[entity_type]}
    record.update(id=str(header["entity_id"]), version=header["version"],
                  created_at=fmt("instant", header["created_at"]), updated_at=fmt("instant", header["updated_at"]),
                  deleted_at=fmt("instant?", header["deleted_at"]))
    return record


_CHILD_ORDER = {
    "task_revision_tags": ("position",), "task_revision_preferred_dates": ("position",),
    "task_revision_dependencies": ("position",), "task_revision_recurrence_weekdays": ("weekday",),
    "preference_revision_category_multipliers": ("category",), "preference_revision_category_windows": ("category",),
    "preference_revision_tag_relations": ("tag",), "preference_revision_related_tags": ("tag", "position"),
    "execution_revision_sessions": ("position",),
}
_CHILDREN_OF = {
    "task": ("task_revision_tags", "task_revision_preferred_dates", "task_revision_dependencies",
             "task_revision_recurrence_weekdays"),
    "preference": ("preference_revision_category_multipliers", "preference_revision_category_windows",
                   "preference_revision_tag_relations", "preference_revision_related_tags"),
    "execution": ("execution_revision_sessions",),
}


def load_revisions(connection, keys: list[tuple]) -> dict[tuple, dict]:
    """The API records of the stored revisions `keys` ((user_id, id) pairs), read back from their rows."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    headers = {(row.user_id, row.id): row._mapping for row in connection.execute(
        sa.select(record_revisions).where(sa.tuple_(record_revisions.c.user_id, record_revisions.c.id).in_(keys)))}
    by_type = defaultdict(list)
    for key, header in headers.items():
        by_type[header["entity_type"]].append(key)
    records = {}
    for entity_type, type_keys in by_type.items():
        table = DETAIL[entity_type]
        details = {(row.user_id, row.id): row._mapping for row in connection.execute(
            sa.select(table).where(sa.tuple_(table.c.user_id, table.c.id).in_(type_keys)))}
        children: dict = defaultdict(lambda: defaultdict(list))
        for name in _CHILDREN_OF.get(entity_type, ()):
            child = REVISION_CHILDREN[name]
            for row in connection.execute(
                sa.select(child).where(sa.tuple_(child.c.user_id, child.c.revision_id).in_(type_keys))
                .order_by(*(child.c[column] for column in _CHILD_ORDER[name]))
            ):
                children[(row.user_id, row.revision_id)][name].append(row._mapping)
        for key in type_keys:
            if key not in details:
                fail(f"record_revisions (user_id={key[0]}, id={key[1]})", "the typed revision row is missing")
            records[key] = revision_json(entity_type, headers[key], details[key], children[key])
    return records


# -----------------------------------------------------------------------------
# Sync outcomes
# -----------------------------------------------------------------------------


def outcome_rows(user_id, op_id, status, result, where: str) -> tuple[dict, list[tuple]]:
    """A 0003 sync result -> (sync_operations outcome columns, [(table, row), ...] of revisions and problems)."""
    expected = ("op_id", "status", "record" if status == "applied" else "error")
    result = exact_keys(result, expected, where, "the sync result")
    if result["op_id"] != str(op_id) or result["status"] != status:
        fail(where, "the sync result does not belong to its operation")
    columns = {name: None for name in (
        "record_revision_id", "error_code", "error_message", "error_supplied_version", "error_current_version",
        "error_current_revision_id", "error_conflicting_revision_id", "error_reason", "error_failed_op_id")}
    columns["error_problems_present"] = False
    rows: list[tuple] = []

    def snapshot(record, role: str) -> uuid.UUID:
        rid = revision_id("sync_operations", user_id, op_id, role)
        rows.extend(revision_rows(user_id, rid, entity_type_of(record, where), record, f"{where} ({role})"))
        return rid

    if status == "applied":
        columns["record_revision_id"] = snapshot(result["record"], "record")
        return columns, rows
    error = result["error"]
    if not isinstance(error, dict) or not {"code", "message"} <= set(error):
        fail(where, "the sync error has no code and message")
    details = dict(error)
    columns["error_code"] = parse("str", details.pop("code"), where, "error.code")
    columns["error_message"] = parse("str", details.pop("message"), where, "error.message")
    if "supplied_version" in details or "current_version" in details:
        if "current_version" not in details or "supplied_version" not in details:
            fail(where, "the sync error has only one of supplied_version/current_version")
        columns["error_supplied_version"] = parse("int?", details.pop("supplied_version"), where, "supplied_version")
        columns["error_current_version"] = parse("int", details.pop("current_version"), where, "current_version")
    if "current" in details:
        columns["error_current_revision_id"] = snapshot(details.pop("current"), "current")
    if "conflicting" in details:
        columns["error_conflicting_revision_id"] = snapshot(details.pop("conflicting"), "conflicting")
    if "reason" in details:
        columns["error_reason"] = parse("str", details.pop("reason"), where, "error.reason")
    if "failed_op_id" in details:
        columns["error_failed_op_id"] = parse("uuid", details.pop("failed_op_id"), where, "error.failed_op_id")
    if "problems" in details:
        problems = details.pop("problems")
        if not isinstance(problems, list):
            fail(where, "error.problems is not a list")
        columns["error_problems_present"] = True
        for position, problem in enumerate(problems):
            problem = exact_keys(problem, ("location", "message"), where, "a validation problem")
            rows.append((sync_operation_problems, {"user_id": user_id, "op_id": op_id, "position": position,
                                                   "message": parse("str", problem["message"], where, "problem")}))
            for index, part in enumerate(string_list(problem["location"], where, "problem location")):
                rows.append((sync_operation_problem_locations, {"user_id": user_id, "op_id": op_id,
                                                                "problem_position": position, "position": index,
                                                                "part": part}))
    if details:
        fail(where, f"the sync error has details this schema cannot store ({', '.join(sorted(details))})")
    return columns, rows


def outcome_json(row, records: dict, problems: list, locations: dict) -> dict:
    """A stored outcome -> the 0003 sync result (the shape push returns)."""
    result = {"op_id": str(row["op_id"]), "status": row["status"]}
    key = row["user_id"]
    if row["status"] == "applied":
        result["record"] = records[(key, row["record_revision_id"])]
        return result
    error = {"code": row["error_code"], "message": row["error_message"]}
    if row["error_current_version"] is not None:
        error["supplied_version"] = row["error_supplied_version"]
        error["current_version"] = row["error_current_version"]
    if row["error_current_revision_id"] is not None:
        error["current"] = records[(key, row["error_current_revision_id"])]
    if row["error_conflicting_revision_id"] is not None:
        error["conflicting"] = records[(key, row["error_conflicting_revision_id"])]
    if row["error_reason"] is not None:
        error["reason"] = row["error_reason"]
    if row["error_problems_present"]:
        error["problems"] = [{"location": locations.get(problem["position"], []), "message": problem["message"]}
                             for problem in problems]
    if row["error_failed_op_id"] is not None:
        error["failed_op_id"] = str(row["error_failed_op_id"])
    result["error"] = error
    return result


def outcome_revision_keys(row) -> list[tuple]:
    return [(row["user_id"], row[name]) for name in (
        "record_revision_id", "error_current_revision_id", "error_conflicting_revision_id") if row[name] is not None]


def load_outcomes(connection, rows: list) -> dict[tuple, dict]:
    """The sync results of stored outcome rows (sync_operations mappings), read back from their rows."""
    if not rows:
        return {}
    records = load_revisions(connection, [key for row in rows for key in outcome_revision_keys(row)])
    keys = [(row["user_id"], row["op_id"]) for row in rows if row["error_problems_present"]]
    problems, locations = defaultdict(list), defaultdict(lambda: defaultdict(list))
    if keys:
        for problem in connection.execute(
            sa.select(sync_operation_problems).where(
                sa.tuple_(sync_operation_problems.c.user_id, sync_operation_problems.c.op_id).in_(keys))
            .order_by(sync_operation_problems.c.position)
        ):
            problems[(problem.user_id, problem.op_id)].append(problem._mapping)
        for part in connection.execute(
            sa.select(sync_operation_problem_locations).where(sa.tuple_(
                sync_operation_problem_locations.c.user_id, sync_operation_problem_locations.c.op_id).in_(keys))
            .order_by(sync_operation_problem_locations.c.problem_position, sync_operation_problem_locations.c.position)
        ):
            locations[(part.user_id, part.op_id)][part.problem_position].append(part.part)
    return {(row["user_id"], row["op_id"]): outcome_json(
        row, records, problems[(row["user_id"], row["op_id"])], locations[(row["user_id"], row["op_id"])])
        for row in rows}


# -----------------------------------------------------------------------------
# Batching and writing
# -----------------------------------------------------------------------------


def batches(connection, table: sa.Table, keys: tuple[str, ...], *, where=None, size: int | None = None) -> Iterator[list]:
    """The rows of `table` in keyset-paginated batches of at most `size` (bounded memory for any table size)."""
    size = size or BATCH_SIZE
    columns = [table.c[name] for name in keys]
    last = None
    while True:
        query = sa.select(table).order_by(*columns).limit(size)
        if where is not None:
            query = query.where(where)
        if last is not None:
            query = query.where(sa.tuple_(*columns) > sa.tuple_(*(sa.literal(value, type_=column.type)
                                                                   for value, column in zip(last, columns))))
        rows = [row._mapping for row in connection.execute(query)]
        if not rows:
            return
        yield rows
        last = tuple(rows[-1][name] for name in keys)


def insert_rows(connection, rows: list[tuple], where: str) -> None:
    """Insert [(table, row), ...] grouped per table, parents first; a constraint failure is reported without values."""
    grouped: dict[sa.Table, list] = defaultdict(list)
    for table, row in rows:
        grouped[table].append(row)
    for table in WRITTEN_TABLES:
        if grouped.get(table):
            try:
                connection.execute(sa.insert(table), grouped[table])
            except IntegrityError as error:
                constraint = getattr(getattr(getattr(error, "orig", None), "diag", None), "constraint_name", None)
                fail(where, f"converted {table.name} rows violate a database constraint"
                            f"{f' ({constraint})' if constraint else ''}")


def update_rows(connection, table: sa.Table, keys: tuple[str, ...], rows: list[dict]) -> None:
    """UPDATE `table` SET <the other columns of each row> WHERE <keys> -- one executemany per batch."""
    if not rows:
        return
    columns = [name for name in rows[0] if name not in keys]
    statement = sa.update(table).where(*(table.c[key] == sa.bindparam(f"k_{key}", type_=table.c[key].type)
                                         for key in keys))
    statement = statement.values({name: sa.bindparam(f"v_{name}", type_=table.c[name].type) for name in columns})
    connection.execute(statement, [{**{f"k_{key}": row[key] for key in keys},
                                    **{f"v_{name}": row[name] for name in columns}} for row in rows])
