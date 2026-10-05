"""The normalized-storage migrations (0004 expand, 0005 backfill, 0006 validate
and contract) on a *populated* revision-0003 database.

The fixture writes rows exactly as the 0003 server stored them -- JSON tags,
preferred dates and recurrence, JSON preference documents in every
absent/value/explicit-clear state, JSON change-log payloads and sync results
(applied, conflict with `current`/`conflicting` snapshots, rejected with and
without problems) -- plus placements, canonical and historical executions,
work sessions and tombstones. After the upgrade every record, every change-log
entry and every recorded sync outcome must read back semantically equal
(instants compared as instants), a replayed sync operation must keep
answering the same after later edits, and invalid data must stop the upgrade
without changing anything or echoing the offending value.

Runs on SQLite by default and on the disposable PostgreSQL database with
BACKEND_TESTS_ON_POSTGRES=1 (see tests/backend/conftest.py).
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.planning.models import derived_task_type_id
from backend import models
from backend.app import create_app
from backend.database import JSONDocument, UTCDateTime, create_backend_engine
from backend.executions import ExecutionOut
from backend.migrate import current_revision, downgrade, head_revision, upgrade
from backend.migrations import normalized_storage
from backend.migrations.normalized_storage import MigrationDataError
from backend.resources import CRUD_RESOURCES
from backend.security import hash_password
from backend.settings import BackendSettings
from backend.sync import SyncOperationIn
from tests.backend.conftest import PASSWORD, TEST_SECRET, FakeClock, _postgres_url, login_headers

ALICE, BOB = uuid.UUID("a11ce000-0000-4000-8000-000000000001"), uuid.UUID("b0b00000-0000-4000-8000-000000000002")
P1 = "00000000-0000-4000-8000-0000000000a1"
T1, T2, T3 = ("00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2",
              "00000000-0000-4000-8000-0000000000b3")
PL1, PL2, PL3 = ("00000000-0000-4000-8000-0000000000c1", "00000000-0000-4000-8000-0000000000c2",
                 "00000000-0000-4000-8000-0000000000c3")
E1, E2, E3, E4, E5 = (f"00000000-0000-4000-8000-0000000000d{i}" for i in range(1, 6))
FB1, G1 = "00000000-0000-4000-8000-0000000000e1", "00000000-0000-4000-8000-0000000000f1"
PREF_USER, PREF_DAY, PREF_OLD = (f"00000000-0000-4000-8000-00000000009{i}" for i in range(1, 4))
UNKNOWN_TASK, UNKNOWN_PLACEMENT = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-8999-aaaaaaaaaaaa"

JAN = "2026-03-01T08:00:00Z"


def meta(record_id: str, version: int = 1, updated: str = JAN, deleted: str | None = None) -> dict:
    return {"id": record_id, "version": version, "created_at": JAN, "updated_at": updated, "deleted_at": deleted}


def reward(**values) -> dict:
    fields = ("weight_importance", "weight_time_bonus", "weight_tag_relation", "weight_fragmentation_penalty",
              "weight_category_bonus", "max_time_distance_minutes", "same_tag_window_minutes",
              "min_gap_between_tasks_minutes", "short_gap_bonus_weight", "short_gap_bonus_max_minutes",
              "short_gap_bonus_cap")
    return {**{name: None for name in fields}, "tag_relations": None, **values}


# -- the records, in API JSON form (what the 0003 server returned and logged) --------------------

PROJECT = {"name": "Thesis", "description": None, **meta(P1)}
TASK1_V1 = {
    "project_id": P1, "name": "Draft", "category": "study", "tags": ["deep"], "estimated_duration_minutes": 60,
    "priority": 5, "required": False, "required_date": None, "preferred_dates": [], "preferred_time_window": None,
    "dependency_ids": [], "deadline": None, "recurrence": None, **meta(T1),
}
TASK1 = {
    **TASK1_V1, "name": "Thesis chapter", "tags": ["deep", "math", "deep"], "estimated_duration_minutes": 90,
    "priority": 8, "required": True, "preferred_dates": ["2026-03-04", "2026-03-02", "2026-03-04"],
    "preferred_time_window": {"start_minute": 540, "end_minute": 720}, "deadline": "2026-03-05T17:00:00-05:00",
    "recurrence": {"frequency": "weekly", "interval": 2, "weekdays": [0, 2, 4], "day_of_month": None,
                   "end_date": "2026-06-01", "count": None},
    **meta(T1, 2, "2026-03-01T09:30:00.250000Z"),
}
TASK2 = {
    "project_id": None, "name": "Review", "category": "work", "tags": [], "estimated_duration_minutes": 30,
    "priority": 3, "required": False, "required_date": "2026-03-03", "preferred_dates": [],
    "preferred_time_window": None, "dependency_ids": [T1], "deadline": "2026-03-06T12:00:00Z",
    "recurrence": {"frequency": "monthly", "interval": 1, "weekdays": None, "day_of_month": 31, "end_date": None,
                   "count": 3},
    **meta(T2),
}
TASK3_V1 = {
    "project_id": None, "name": "Old", "category": "rest", "tags": ["x"], "estimated_duration_minutes": 15,
    "priority": 1, "required": False, "required_date": None, "preferred_dates": ["2026-02-01"],
    "preferred_time_window": None, "dependency_ids": [], "deadline": None,
    "recurrence": {"frequency": "daily", "interval": 1, "weekdays": None, "day_of_month": None, "end_date": None,
                   "count": None},
    **meta(T3),
}
TASK3 = {**TASK3_V1, **meta(T3, 2, "2026-03-01T10:00:00Z", deleted="2026-03-01T10:00:00Z")}
BLOCK = {"label": "Sleep", "category": "sleep", "planned_date": "2026-03-02", "timezone": "UTC",
         "planned_start": "2026-03-02T00:00:00Z", "planned_end": "2026-03-02T07:00:00Z", **meta(FB1)}
PLACEMENT1 = {"task_id": T1, "planned_date": "2026-03-02", "timezone": "Europe/Berlin",
              "planned_start": "2026-03-02T09:00:00Z", "planned_end": "2026-03-02T10:30:00Z", "score": 12.3456789,
              "optimization_metadata": {"mode": "precise", "candidates": [1, 2.5, None], "nested": {"ok": True}},
              **meta(PL1)}
#: The change log recorded the placement with the client's offset; the same instants as PLACEMENT1.
PLACEMENT1_LOGGED = {**PLACEMENT1, "planned_start": "2026-03-02T10:00:00+01:00",
                     "planned_end": "2026-03-02T11:30:00+01:00"}
PLACEMENT2 = {**PLACEMENT1, "task_id": T2, "planned_date": "2026-03-03", "planned_start": "2026-03-03T09:00:00Z",
              "planned_end": "2026-03-03T09:30:00Z", "score": 0.0, "optimization_metadata": {}, **meta(PL2)}
PLACEMENT3 = {**PLACEMENT2, "task_id": T3, "planned_date": "2026-02-01", "planned_start": "2026-02-01T09:00:00Z",
              "planned_end": "2026-02-01T09:15:00Z", **meta(PL3, 2, "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z")}
USER_OVERRIDES = {
    "day_window": {"start_minute": 360, "end_minute": 1320, "end_day_offset": 0},
    "category_multipliers": {"study": 2.0, "work": None, "zero": 0.0},
    "category_preferred_windows": {"study": {"start_minute": 540, "end_minute": 720}, "rest": None},
    "optimizer_mode": "adhd_friendly",
    "reward": reward(weight_importance=6.5, max_time_distance_minutes=180,
                     tag_relations={"math": ["physics", "math", "physics"], "solo": []}),
}
DAY_OVERRIDES = {"day_window": None, "category_multipliers": {}, "category_preferred_windows": {},
                 "optimizer_mode": None, "reward": reward(tag_relations={})}
OLD_OVERRIDES = {"day_window": None, "category_multipliers": {"x": 1.5}, "category_preferred_windows": {},
                 "optimizer_mode": None, "reward": reward()}
PREF_USER_RECORD = {"scope": "user", "date": None, "overrides": USER_OVERRIDES, **meta(PREF_USER)}
PREF_DAY_RECORD = {"scope": "date", "date": "2026-03-02", "overrides": DAY_OVERRIDES, **meta(PREF_DAY)}
PREF_OLD_RECORD = {"scope": "date", "date": "2026-03-03", "overrides": OLD_OVERRIDES,
                   **meta(PREF_OLD, 2, "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z")}
GENERATION = {"planned_date": "2026-03-02", "timezone": "UTC", "engine_mode": "precise_greedy",
              "range_start": "2026-03-02", "range_end": "2026-03-08", "range_scope": "planned",
              "allocation_id": "11111111-1111-4111-8111-111111111111", "fingerprint": "f" * 64,
              "fingerprint_version": 1, "placements_digest": "d" * 64, "placement_count": 1,
              "unscheduled_count": 0, "total_score": 12.35, "generated_at": "2026-03-02T11:00:00Z", **meta(G1)}


def execution(record_id: str, **values) -> dict:
    base = {
        "legacy_id": None, "task_id": None, "scheduled_task_id": None, "historical_reference": False,
        "task_name": "Thesis chapter", "category": "study", "tag": "deep", "planned_date": None,
        "planned_start": None, "planned_end": None, "planned_duration": 90, "priority": 8, "status": "scheduled",
        "sessions": [], "actual_active_duration_minutes": None, "duration_variance_minutes": None,
        "start_delay_minutes": None, "focus_rating": None, "energy_rating": None, "interruption_count": None,
        "note": None, "canonical_planned_date": None, "canonical_timezone": None, "canonical_planned_start": None,
        "canonical_planned_end": None, "actual_first_start_at": None, "actual_final_end_at": None,
    }
    return {**base, **meta(record_id), **values}


EXEC1 = execution(
    E1, task_id=T1, scheduled_task_id=PL1, status="completed", version=3, updated_at="2026-03-02T10:40:00Z",
    sessions=[{"started_at": "2026-03-02T09:05:00Z", "ended_at": "2026-03-02T09:50:00Z"},
              {"started_at": "2026-03-02T10:00:00Z", "ended_at": "2026-03-02T10:40:00Z"}],
    actual_active_duration_minutes=85.0, duration_variance_minutes=-5.0, start_delay_minutes=5.0, focus_rating=4,
    note="went well", canonical_planned_date="2026-03-02", canonical_timezone="Europe/Berlin",
    canonical_planned_start="2026-03-02T09:00:00Z", canonical_planned_end="2026-03-02T10:30:00Z",
    actual_first_start_at="2026-03-02T09:05:00Z", actual_final_end_at="2026-03-02T10:40:00Z",
)
EXEC2 = execution(E2, task_id=UNKNOWN_TASK, scheduled_task_id=UNKNOWN_PLACEMENT, historical_reference=True,
                  task_name="Never persisted", planned_date=3, planned_start=540, planned_end=600, planned_duration=60,
                  status="skipped")
EXEC3 = execution(E3, task_id=T3, scheduled_task_id=PL3, historical_reference=True, task_name="Old", status="cancelled")
EXEC4 = execution(E4, legacy_id="legacy-7", historical_reference=True, task_name="Imported", status="completed",
                  interruption_count=2)
EXEC5 = execution(E5, task_id=T2, status="in_progress", version=2,
                  sessions=[{"started_at": "2026-03-02T12:00:00Z", "ended_at": None}],
                  actual_first_start_at="2026-03-02T12:00:00Z")

#: seq -> (entity_type, operation, record) as the 0003 server logged them (tombstones and histories included).
CHANGES = [
    ("project", "upsert", PROJECT), ("task", "upsert", TASK1_V1), ("task", "upsert", TASK1),
    ("task", "upsert", TASK2), ("task", "upsert", TASK3_V1), ("task", "delete", TASK3),
    ("placement", "upsert", PLACEMENT1_LOGGED), ("placement", "upsert", PLACEMENT2),
    ("fixed_block", "upsert", BLOCK), ("preference", "upsert", PREF_USER_RECORD),
    ("preference", "upsert", PREF_DAY_RECORD), ("preference", "delete", PREF_OLD_RECORD),
    ("execution", "upsert", EXEC1), ("execution", "upsert", EXEC5), ("schedule_generation", "upsert", GENERATION),
]


def content(record: dict) -> dict:
    return {name: value for name, value in record.items() if name not in ("id", "version", "created_at",
                                                                          "updated_at", "deleted_at")}


def sync_op(entity_type: str, kind: str, entity_id: str, base_version=None, payload=None, **extra) -> dict:
    return {"op_id": str(uuid.uuid4()), "entity_type": entity_type, "entity_id": entity_id, "kind": kind,
            "base_version": base_version, "payload": payload, **extra}


#: Replayable pushed operations and the results the 0003 server recorded for them.
OP_CREATE = sync_op("task", "create", T2, payload=content(TASK2))
OP_STALE = sync_op("task", "update", T1, base_version=1, payload={**content(TASK1_V1), "name": "Mine"})
OP_DELETED = sync_op("task", "update", T3, base_version=1, payload=content(TASK3_V1))
OP_OVERLAP = sync_op("fixed_block", "create", str(uuid.uuid4()), payload=content(BLOCK))
OP_PROBLEMS = sync_op("task", "create", str(uuid.uuid4()), payload={"category": "x"})
OP_EMPTY = sync_op("project", "create", str(uuid.uuid4()), payload={"name": "y"})
OP_REASON = sync_op("fixed_block", "create", str(uuid.uuid4()), payload={**content(BLOCK), "label": "Late"})
OP_GROUP = sync_op("project", "create", str(uuid.uuid4()), payload={"name": "z"}, group=str(uuid.uuid4()))
OP_EXECUTION = sync_op("execution", "create", E1, payload=content(EXEC1))
OP_REFERENCE = sync_op("placement", "create", str(uuid.uuid4()), payload=content(PLACEMENT1) | {"task_id": UNKNOWN_TASK})
RESULTS = [
    (OP_CREATE, {"op_id": OP_CREATE["op_id"], "status": "applied", "record": TASK2}),
    (OP_STALE, {"op_id": OP_STALE["op_id"], "status": "conflict", "error": {
        "code": "version_conflict", "message": "The task was changed since version 1.",
        "supplied_version": 1, "current_version": 2, "current": TASK1}}),
    (OP_DELETED, {"op_id": OP_DELETED["op_id"], "status": "conflict", "error": {
        "code": "deleted", "message": "The task has been deleted.", "supplied_version": 1, "current_version": 2,
        "current": TASK3}}),
    (OP_OVERLAP, {"op_id": OP_OVERLAP["op_id"], "status": "conflict", "error": {
        "code": "fixed_block_overlap", "message": "The fixed block overlaps your fixed block 'Sleep' on 2026-03-02.",
        "conflicting": BLOCK}}),
    (OP_PROBLEMS, {"op_id": OP_PROBLEMS["op_id"], "status": "rejected", "error": {
        "code": "validation_error", "message": "The operation is not valid.", "problems": [
            {"location": ["name"], "message": "Field required"},
            {"location": [], "message": "Value error, something else"}]}}),
    (OP_EMPTY, {"op_id": OP_EMPTY["op_id"], "status": "rejected", "error": {
        "code": "validation_error", "message": "The operation is not valid.", "problems": []}}),
    (OP_REASON, {"op_id": OP_REASON["op_id"], "status": "rejected", "error": {
        "code": "validation_error", "message": "outside the day window", "reason": "outside_day_window"}}),
    (OP_GROUP, {"op_id": OP_GROUP["op_id"], "status": "conflict", "error": {
        "code": "group_failed", "message": "Operation x of this group failed.", "failed_op_id": OP_STALE["op_id"]}}),
    (OP_EXECUTION, {"op_id": OP_EXECUTION["op_id"], "status": "applied", "record": EXEC1}),
    (OP_REFERENCE, {"op_id": OP_REFERENCE["op_id"], "status": "rejected", "error": {
        "code": "invalid_reference", "message": "task_id does not name one of your tasks."}}),
]


# -----------------------------------------------------------------------------
# Writing the revision-0003 rows
# -----------------------------------------------------------------------------

_md = sa.MetaData()
U, J, T = sa.Uuid(), JSONDocument, UTCDateTime()


def _t(name: str, *columns) -> sa.Table:
    return sa.Table(name, _md, *(sa.Column(column, kind) for column, kind in columns))


AUDIT = (("user_id", U), ("id", U), ("created_at", T), ("updated_at", T), ("version", sa.Integer()),
         ("deleted_at", T))
OLD = {
    "users": _t("users", ("id", U), ("email", sa.Text()), ("password_hash", sa.Text()), ("change_seq", sa.Integer()),
                ("created_at", T), ("updated_at", T), ("version", sa.Integer())),
    "projects": _t("projects", *AUDIT, ("name", sa.Text()), ("description", sa.Text())),
    "tasks": _t("tasks", *AUDIT, *((name, sa.Text()) for name in ("name", "category", "deadline")),
                ("project_id", U), ("tags", J), ("estimated_duration_minutes", sa.Integer()),
                ("priority", sa.Integer()), ("required", sa.Boolean()), ("required_date", sa.Date()),
                ("preferred_dates", J), ("preferred_window_start_minute", sa.Integer()),
                ("preferred_window_end_minute", sa.Integer()), ("deadline_utc", T), ("recurrence", J)),
    "task_dependencies": _t("task_dependencies", ("user_id", U), ("task_id", U), ("position", sa.Integer()),
                            ("depends_on_id", U)),
    "fixed_blocks": _t("fixed_blocks", *AUDIT, ("label", sa.Text()), ("category", sa.Text()),
                       ("planned_date", sa.Date()), ("timezone", sa.Text()), ("planned_start", T), ("planned_end", T)),
    "placements": _t("placements", *AUDIT, ("task_id", U), ("planned_date", sa.Date()), ("timezone", sa.Text()),
                     ("planned_start", T), ("planned_end", T), ("score", sa.Float()), ("optimization_metadata", J)),
    "preferences": _t("preferences", *AUDIT, ("scope", sa.Text()), ("scope_date", sa.Date()),
                      ("scope_key", sa.Text()), ("optimizer_mode", sa.Text()), ("overrides", J)),
    "schedule_generations": _t("schedule_generations", *AUDIT, *(
        (name, kind) for name, kind in (
            ("planned_date", sa.Date()), ("timezone", sa.Text()), ("engine_mode", sa.Text()),
            ("range_start", sa.Date()), ("range_end", sa.Date()), ("range_scope", sa.Text()), ("allocation_id", U),
            ("fingerprint", sa.Text()), ("fingerprint_version", sa.Integer()), ("placements_digest", sa.Text()),
            ("placement_count", sa.Integer()), ("unscheduled_count", sa.Integer()), ("total_score", sa.Float()),
            ("generated_at", T)))),
    "executions": _t("executions", *AUDIT, *(
        (name, kind) for name, kind in (
            ("legacy_id", sa.Text()), ("task_id", U), ("scheduled_task_id", U), ("historical_reference", sa.Boolean()),
            ("task_name", sa.Text()), ("category", sa.Text()), ("tag", sa.Text()), ("planned_date", sa.Integer()),
            ("planned_start", sa.Integer()), ("planned_end", sa.Integer()), ("planned_duration", sa.Integer()),
            ("priority", sa.Integer()), ("status", sa.Text()), ("actual_active_duration_minutes", sa.Float()),
            ("duration_variance_minutes", sa.Float()), ("start_delay_minutes", sa.Float()),
            ("focus_rating", sa.Integer()), ("energy_rating", sa.Integer()), ("interruption_count", sa.Integer()),
            ("note", sa.Text()), ("canonical_planned_date", sa.Date()), ("canonical_timezone", sa.Text()),
            ("canonical_planned_start", T), ("canonical_planned_end", T), ("actual_first_start_at", T),
            ("actual_final_end_at", T)))),
    "work_sessions": _t("work_sessions", ("user_id", U), ("execution_id", U), ("position", sa.Integer()),
                        ("started_at", T), ("ended_at", T)),
    "change_log": _t("change_log", ("user_id", U), ("seq", sa.BigInteger()), ("entity_type", sa.Text()),
                     ("entity_id", U), ("operation", sa.Text()), ("version", sa.Integer()), ("recorded_at", T),
                     ("payload", J)),
    "sync_operations": _t("sync_operations", ("user_id", U), ("op_id", U), ("request_hash", sa.Text()),
                          ("status", sa.Text()), ("result", J), ("recorded_at", T)),
}


def _instant(value):
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _day(value):
    return None if value is None else datetime.fromisoformat(value).date()


def _audit(user_id, record: dict) -> dict:
    return {"user_id": user_id, "id": uuid.UUID(record["id"]), "version": record["version"],
            "created_at": _instant(record["created_at"]), "updated_at": _instant(record["updated_at"]),
            "deleted_at": _instant(record["deleted_at"])}


def _task_row(user_id, record: dict) -> dict:
    window = record["preferred_time_window"] or {}
    return {**_audit(user_id, record), "project_id": record["project_id"] and uuid.UUID(record["project_id"]),
            "name": record["name"], "category": record["category"], "tags": record["tags"],
            "estimated_duration_minutes": record["estimated_duration_minutes"], "priority": record["priority"],
            "required": record["required"], "required_date": _day(record["required_date"]),
            "preferred_dates": record["preferred_dates"], "preferred_window_start_minute": window.get("start_minute"),
            "preferred_window_end_minute": window.get("end_minute"),
            "deadline": _instant(record["deadline"]).isoformat() if record["deadline"] else None,
            "deadline_utc": _instant(record["deadline"]), "recurrence": record["recurrence"]}


def _placement_row(user_id, record: dict) -> dict:
    return {**_audit(user_id, record), "task_id": uuid.UUID(record["task_id"]),
            "planned_date": _day(record["planned_date"]), "timezone": record["timezone"],
            "planned_start": _instant(record["planned_start"]), "planned_end": _instant(record["planned_end"]),
            "score": record["score"], "optimization_metadata": record["optimization_metadata"]}


def _preference_row(user_id, record: dict, document: dict) -> dict:
    return {**_audit(user_id, record), "scope": record["scope"], "scope_date": _day(record["date"]),
            "scope_key": record["date"] or "user", "optimizer_mode": record["overrides"]["optimizer_mode"],
            "overrides": document}


def _execution_row(user_id, record: dict) -> dict:
    row = {**_audit(user_id, record)}
    for name, value in record.items():
        if name in row or name == "id" or name == "sessions":
            continue
        column = OLD["executions"].c[name].type
        if name in ("task_id", "scheduled_task_id"):
            value = value and uuid.UUID(value)
        elif isinstance(column, UTCDateTime):
            value = _instant(value)
        elif isinstance(column, sa.Date):
            value = _day(value)
        row[name] = value
    return row


def populate_0003(connection) -> None:
    now = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
    password = hash_password(PASSWORD)

    def insert(table: str, *rows) -> None:
        connection.execute(sa.insert(OLD[table]), list(rows))

    insert("users", *({"id": user, "email": email, "password_hash": password, "change_seq": seq, "created_at": now,
                       "updated_at": now, "version": 1}
                      for user, email, seq in ((ALICE, "alice@example.com", len(CHANGES)), (BOB, "bob@example.com", 0))))
    insert("projects", {**_audit(ALICE, PROJECT), "name": "Thesis", "description": None})
    insert("tasks", *(_task_row(ALICE, record) for record in (TASK1, TASK2, TASK3)))
    insert("task_dependencies", {"user_id": ALICE, "task_id": uuid.UUID(T2), "position": 0,
                                 "depends_on_id": uuid.UUID(T1)})
    insert("fixed_blocks", {**_audit(ALICE, BLOCK), **{name: BLOCK[name] for name in ("label", "category", "timezone")},
                            "planned_date": _day(BLOCK["planned_date"]), "planned_start": _instant(BLOCK["planned_start"]),
                            "planned_end": _instant(BLOCK["planned_end"])})
    insert("placements", *(_placement_row(ALICE, record) for record in (PLACEMENT1, PLACEMENT2, PLACEMENT3)))
    # The live preferences.overrides document had no optimizer_mode (a column); the old date layer's document
    # was written with keys omitted, which mean their defaults.
    insert("preferences",
           _preference_row(ALICE, PREF_USER_RECORD, {k: v for k, v in USER_OVERRIDES.items() if k != "optimizer_mode"}),
           _preference_row(ALICE, PREF_DAY_RECORD, {k: v for k, v in DAY_OVERRIDES.items() if k != "optimizer_mode"}),
           _preference_row(ALICE, PREF_OLD_RECORD, {"category_multipliers": {"x": 1.5}}))
    insert("schedule_generations", {**_audit(ALICE, GENERATION), **{
        name: _day(GENERATION[name]) if name in ("planned_date", "range_start", "range_end")
        else _instant(GENERATION[name]) if name == "generated_at"
        else uuid.UUID(GENERATION[name]) if name == "allocation_id" else GENERATION[name]
        for name in content(GENERATION)}})
    insert("executions", *(_execution_row(ALICE, record) for record in (EXEC1, EXEC2, EXEC3, EXEC4, EXEC5)))
    insert("work_sessions", *({"user_id": ALICE, "execution_id": uuid.UUID(record["id"]), "position": position,
                               "started_at": _instant(work["started_at"]), "ended_at": _instant(work["ended_at"])}
                              for record in (EXEC1, EXEC5) for position, work in enumerate(record["sessions"])))
    insert("change_log", *({"user_id": ALICE, "seq": seq, "entity_type": entity_type,
                            "entity_id": uuid.UUID(record["id"]), "operation": operation, "version": record["version"],
                            "recorded_at": now, "payload": record}
                           for seq, (entity_type, operation, record) in enumerate(CHANGES, start=1)))
    insert("sync_operations", *({"user_id": ALICE, "op_id": uuid.UUID(op["op_id"]),
                                 "request_hash": SyncOperationIn.model_validate(op).request_hash(),
                                 "status": result["status"], "result": result, "recorded_at": now}
                                for op, result in RESULTS))


# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def blank_engine():
    """An empty database (SQLite, or a private schema of the disposable PostgreSQL database)."""
    url = _postgres_url()
    if url is None:
        engine = create_backend_engine("sqlite://")
        yield engine
        engine.dispose()
        return
    schema = f"sm_test_{uuid.uuid4().hex[:12]}"
    admin = create_backend_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_backend_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def predecessor(blank_engine):
    """A revision-0003 database with the populated fixture data."""
    upgrade(blank_engine, "0003")
    with blank_engine.begin() as connection:
        populate_0003(connection)
    return blank_engine


def client_for(engine, clock=None) -> TestClient:
    settings = BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET)
    return TestClient(create_app(settings, engine=engine, clock=clock or FakeClock()))


def as_instants(value):
    """JSON with every ISO instant string replaced by the instant (so equal instants compare equal)."""
    if isinstance(value, dict):
        return {key: as_instants(item) for key, item in value.items()}
    if isinstance(value, list):
        return [as_instants(item) for item in value]
    if isinstance(value, str) and "T" in value and value[:4].isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return parsed if parsed.tzinfo else value
    return value


#: Placement fields added by revision 0007 (docs/execution-rescheduling.md). A record written before it has
#: them all null: an unknown category snapshot and removal provenance are never back-filled or guessed.
PLACEMENT_FIELDS_SINCE_0007 = {"task_category": None, "removal_reason": None, "superseded_by_id": None}
#: 0008: an existing task gets the default points; an existing execution's snapshot is unknown.
TASK_FIELDS_SINCE_0008 = {"points": 1}
EXECUTION_FIELDS_SINCE_0008 = {"points": None}
#: 0009 (docs/recurrence.md): no existing task is an occurrence, a segment or a configured series.
TASK_FIELDS_SINCE_0009 = {"series_id": None, "occurrence_slot": None, "occurrence_state": None, "series_version": None,
                          "series_predecessor_id": None}
RECURRENCE_FIELDS_SINCE_0009 = {"start_date": None, "timezone": None}
#: 0010 (docs/execution-rescheduling.md): an existing placement's origin is unknown and it carries no manual intent
#: (a proven move is recognized from its lineage when read, not rewritten); an execution's cancel reason is unknown.
PLACEMENT_FIELDS_SINCE_0010 = {"origin": None, "preserved": False}
EXECUTION_FIELDS_SINCE_0010 = {"cancel_reason": None}
#: 0013 (docs/productivity-redesign-plan.md): a historical task record has no type and an existing placement no
#: planning snapshot (unknown, never back-filled). A *live* task gets its deterministic type (live_task_at_head).
TASK_FIELDS_SINCE_0013 = {"task_type_id": None}
PLACEMENT_FIELDS_SINCE_0013 = {"task_name": None, "task_tags": None, "task_points": None,
                               "task_estimate_minutes": None, "task_type_id": None, "task_type_label": None}


def live_task_at_head(record: dict) -> dict:
    """A fixture task as its live row reads at head: 0013 derived its type from its own id (no series here)."""
    return {**record, "task_type_id": str(derived_task_type_id(uuid.UUID(record["id"])))}


def as_of_head(value):
    """
    The fixture (as the 0003 API wrote it) as today's API returns it:
    placements gain the 0007 fields (null), tasks their 0008 points (the
    default) and 0009 recurrence fields (null), recurrence rules their 0009
    anchor (null: needs configuration) and executions their 0008 points
    snapshot (null).
    """
    if isinstance(value, dict):
        converted = {key: as_of_head(item) for key, item in value.items()}
        if {"task_id", "planned_start", "optimization_metadata"} <= set(value):
            converted = {**PLACEMENT_FIELDS_SINCE_0007, **PLACEMENT_FIELDS_SINCE_0010, **PLACEMENT_FIELDS_SINCE_0013,
                         **converted}
        if {"estimated_duration_minutes", "priority", "dependency_ids"} <= set(value):
            converted = {**TASK_FIELDS_SINCE_0008, **TASK_FIELDS_SINCE_0009, **TASK_FIELDS_SINCE_0013, **converted}
        if {"frequency", "interval"} <= set(value):
            converted = {**RECURRENCE_FIELDS_SINCE_0009, **converted}
        if {"task_name", "planned_duration", "status"} <= set(value):
            converted = {**EXECUTION_FIELDS_SINCE_0008, **EXECUTION_FIELDS_SINCE_0010, **converted}
        return converted
    if isinstance(value, list):
        return [as_of_head(item) for item in value]
    return value


def assert_same(actual, expected) -> None:
    assert as_instants(actual) == as_instants(expected)


def assert_same_at_head(actual, expected) -> None:
    """`actual` (read from today's API) equals the fixture record as it looks at the head revision."""
    assert_same(actual, as_of_head(expected))


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_the_fixture_records_are_valid_api_records() -> None:
    """The fixture is what the 0003 API really wrote: every record validates against today's Out schemas."""
    schemas = {spec.entity_type: spec.out_schema for spec in CRUD_RESOURCES} | {"execution": ExecutionOut}
    for entity_type, _, record in CHANGES:
        assert_same_at_head(schemas[entity_type].model_validate(record).model_dump(mode="json"), record)


@pytest.mark.parametrize("batch_size", [500, 2], ids=["one-batch", "many-batches"])
def test_a_populated_0003_database_upgrades_losslessly(predecessor, monkeypatch, batch_size) -> None:
    monkeypatch.setattr(normalized_storage, "BATCH_SIZE", batch_size)  # 2: every table spans several batches
    upgrade(predecessor)
    with predecessor.connect() as connection:
        assert current_revision(connection) == head_revision()

    with client_for(predecessor) as client:
        headers = login_headers(client, "alice@example.com")

        def read(path: str, record: dict) -> None:
            response = client.get(f"/{path}/{record['id']}", params={"include_deleted": True}, headers=headers)
            assert response.status_code == 200, response.text
            assert_same_at_head(response.json(), record)

        read("projects", PROJECT)
        for record in (TASK1, TASK2, TASK3):  # ordered tags/dates with repeats, recurrence, deadline offset, tombstone
            read("tasks", live_task_at_head(record))
        assert client.get(f"/tasks/{T1}", headers=headers).json()["deadline"] == "2026-03-05T17:00:00-05:00"
        for record in (PLACEMENT1, PLACEMENT2, PLACEMENT3):
            read("placements", record)
        read("fixed-blocks", BLOCK)
        for record in (PREF_USER_RECORD, PREF_DAY_RECORD, PREF_OLD_RECORD):  # every absent/value/clear state
            read("preferences", record)
        read("schedule-generations", GENERATION)
        for record in (EXEC1, EXEC2, EXEC3, EXEC4, EXEC5):  # canonical, historical, legacy; sessions kept
            read("executions", record)

        feed = client.get("/changes", params={"limit": 500}, headers=headers).json()
        assert [change["seq"] for change in feed["changes"]] == list(range(1, len(CHANGES) + 1))
        for change, (entity_type, operation, record) in zip(feed["changes"], CHANGES):
            assert (change["entity_type"], change["operation"], change["version"]) == (
                entity_type, operation, record["version"])
            assert_same_at_head(change["record"], record)  # the historical snapshot, not the current row

        # The per-user order continues after the migrated entries; Bob's feed is still empty.
        created = client.post("/projects", json={"name": "After"}, headers=headers).json()
        assert client.get("/changes", params={"after": len(CHANGES)}, headers=headers).json()["changes"][0]["seq"] == \
            len(CHANGES) + 1 and created["version"] == 1
        assert client.get("/changes", headers=login_headers(client, "bob@example.com")).json()["changes"] == []

    with predecessor.connect() as connection:
        links = {str(row.id): (row.linked_task_id, row.linked_placement_id) for row in connection.execute(
            sa.select(models.Execution.id, models.Execution.linked_task_id, models.Execution.linked_placement_id))}
    as_ids = {key: tuple(str(value) if value else None for value in pair) for key, pair in links.items()}
    assert as_ids == {
        E1: (T1, PL1),       # canonical: enforced references
        E2: (None, None),    # never persisted: historical identity only, nothing invented
        E3: (T3, PL3),       # historical, but its (tombstoned) task and placement exist: bound to them
        E4: (None, None),    # legacy id, no task
        E5: (T2, None),      # canonical task link without a placement
    }


def test_recorded_sync_outcomes_replay_identically_even_after_later_edits(predecessor) -> None:
    upgrade(predecessor)
    replayable = [RESULTS[0], RESULTS[1], RESULTS[2], RESULTS[3], RESULTS[4], RESULTS[5], RESULTS[6], RESULTS[8],
                  RESULTS[9]]
    with client_for(predecessor) as client:
        headers = login_headers(client, "alice@example.com")

        def replay() -> list[dict]:
            response = client.post("/sync/push", json={"operations": [op for op, _ in replayable]}, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()["results"]

        first = replay()
        for result, (_, recorded) in zip(first, replayable):
            expected = copy.deepcopy(recorded)
            expected.setdefault("record", None)
            expected.setdefault("related", None)  # only a multi-record operation (a reschedule) has any
            expected.setdefault("error", None)
            assert_same_at_head(result, expected)

        task1 = client.get(f"/tasks/{T1}", headers=headers).json()
        edit = {**content(task1), "name": "Renamed later", "tags": ["new"], "base_version": task1["version"]}
        assert client.put(f"/tasks/{T1}", json=edit, headers=headers).status_code == 200
        task2 = client.get(f"/tasks/{T2}", headers=headers).json()
        assert client.put(f"/tasks/{T2}", json={**content(task2), "priority": 9, "base_version": task2["version"]},
                          headers=headers).status_code == 200

        assert replay() == first  # snapshots are immutable: the later edits change no recorded outcome
        assert first[0]["record"]["name"] == "Review" and first[1]["error"]["current"]["name"] == "Thesis chapter"


def test_invalid_existing_data_stops_the_upgrade_and_changes_nothing(blank_engine) -> None:
    upgrade(blank_engine, "0003")
    with blank_engine.begin() as connection:
        populate_0003(connection)
        connection.execute(sa.update(OLD["tasks"]).where(OLD["tasks"].c.id == uuid.UUID(T2))
                           .values(tags=["ok", {"private": "do-not-echo-4711"}]))

    with pytest.raises(MigrationDataError) as raised:
        upgrade(blank_engine)
    message = str(raised.value)
    assert f"tasks (user_id={ALICE}, id={T2})" in message and "'tags'" in message
    assert "do-not-echo-4711" not in message and "rolled back" in message

    with blank_engine.connect() as connection:
        assert current_revision(connection) == "0003"
        tables = set(sa.inspect(connection).get_table_names())
        assert "task_tags" not in tables and "record_revisions" not in tables
        assert connection.execute(sa.select(OLD["tasks"].c.tags).where(OLD["tasks"].c.id == uuid.UUID(T1))).scalar_one() \
            == TASK1["tags"]


def test_placement_metadata_holding_a_schedule_stops_the_upgrade(blank_engine) -> None:
    upgrade(blank_engine, "0003")
    with blank_engine.begin() as connection:
        populate_0003(connection)
        connection.execute(sa.update(OLD["placements"]).where(OLD["placements"].c.id == uuid.UUID(PL2))
                           .values(optimization_metadata={"schedule": [{"task": index} for index in range(3)]}))
    with pytest.raises(MigrationDataError, match=f"placements \\(user_id={ALICE}, id={PL2}\\)"):
        upgrade(blank_engine)
    with blank_engine.connect() as connection:
        assert current_revision(connection) == "0003"


def test_an_unresolvable_canonical_execution_stops_the_upgrade(blank_engine) -> None:
    upgrade(blank_engine, "0003")
    with blank_engine.begin() as connection:
        populate_0003(connection)
        connection.execute(sa.update(OLD["executions"]).where(OLD["executions"].c.id == uuid.UUID(E2))
                           .values(historical_reference=False))
    with pytest.raises(MigrationDataError, match="historical_reference"):
        upgrade(blank_engine)
    with blank_engine.connect() as connection:
        assert current_revision(connection) == "0003"


def test_an_empty_database_upgrades_and_a_disposable_one_downgrades_losslessly(predecessor) -> None:
    upgrade(predecessor)
    with client_for(predecessor) as client:  # a write made only in the normalized form
        headers = login_headers(client, "alice@example.com")
        created = client.post("/tasks", json={"name": "New", "category": "study", "estimated_duration_minutes": 20,
                                              "priority": 4, "tags": ["b", "a", "b"]}, headers=headers).json()

    downgrade(predecessor, "0003")  # disposable database only
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0003"
        tasks = {str(row.id): row for row in connection.execute(sa.select(OLD["tasks"]))}
        preferences = {str(row.id): row.overrides for row in connection.execute(sa.select(OLD["preferences"]))}
        payloads = [row.payload for row in connection.execute(
            sa.select(OLD["change_log"]).where(OLD["change_log"].c.user_id == ALICE).order_by(OLD["change_log"].c.seq))]
        results = {str(row.op_id): row.result for row in connection.execute(sa.select(OLD["sync_operations"]))}
    for record in (TASK1, TASK2, TASK3):
        row = tasks[record["id"]]
        assert (row.tags, row.preferred_dates, row.recurrence) == (
            record["tags"], record["preferred_dates"], record["recurrence"])
    assert tasks[created["id"]].tags == ["b", "a", "b"]
    assert_same(preferences[PREF_USER], {k: v for k, v in USER_OVERRIDES.items() if k != "optimizer_mode"})
    assert_same(preferences[PREF_OLD], {k: v for k, v in OLD_OVERRIDES.items() if k != "optimizer_mode"})
    for payload, (_, _, record) in zip(payloads, CHANGES):
        assert_same(payload, record)
    for op, recorded in RESULTS:
        assert_same(results[op["op_id"]], recorded)

    upgrade(predecessor)  # and forward again
    with predecessor.connect() as connection:
        assert current_revision(connection) == head_revision()


def test_the_previous_head_upgrades_to_0007_keeping_every_record_and_replay(predecessor) -> None:
    """0006 (the Milestone 4 head, populated) -> 0007 -> head: history, change feed and recorded outcomes survive."""
    upgrade(predecessor, "0006")
    tables = ("placements", "executions", "work_sessions", "change_log", "sync_operations", "record_revisions")
    with predecessor.connect() as connection:
        before = {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in tables}
        users = [tuple(row) for row in connection.execute(sa.text("SELECT id, change_seq FROM users ORDER BY id"))]

    upgrade(predecessor)
    with predecessor.connect() as connection:
        assert current_revision(connection) == head_revision()
        assert {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in tables} == before
        assert [tuple(row) for row in connection.execute(sa.text("SELECT id, change_seq FROM users ORDER BY id"))] == users
        unknown = connection.execute(sa.text(
            "SELECT COUNT(*) FROM placements WHERE task_category IS NOT NULL OR removal_reason IS NOT NULL "
            "OR superseded_by_id IS NOT NULL")).scalar_one()
        assert unknown == 0  # history from before 0007 stays unknown: nothing is back-filled
        assert connection.execute(sa.text("SELECT COUNT(*) FROM sync_operation_related_records")).scalar_one() == 0

    replayable = [RESULTS[0], RESULTS[1], RESULTS[2], RESULTS[3], RESULTS[4], RESULTS[5], RESULTS[6], RESULTS[8],
                  RESULTS[9]]
    with client_for(predecessor) as client:
        headers = login_headers(client, "alice@example.com")
        results = client.post("/sync/push", json={"operations": [op for op, _ in replayable]}, headers=headers).json()
        for result, (_, recorded) in zip(results["results"], replayable):
            expected = copy.deepcopy(recorded)
            expected.setdefault("record", None)
            expected.setdefault("related", None)
            expected.setdefault("error", None)
            assert_same_at_head(result, expected)
        for record in (EXEC1, EXEC2, EXEC3, EXEC4, EXEC5):  # sessions, legacy ids and owners kept
            response = client.get(f"/executions/{record['id']}", params={"include_deleted": True}, headers=headers)
            assert_same_at_head(response.json(), record)


def test_0008_adds_points_without_touching_any_record(predecessor) -> None:
    """0007 -> 0008: every row stays; tasks (and their revisions) get the default points, executions none."""
    upgrade(predecessor, "0007")
    tables = ("tasks", "task_revisions", "executions", "execution_revisions", "change_log", "record_revisions")
    with predecessor.connect() as connection:
        before = {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in tables}
    upgrade(predecessor, "0008")
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0008"
        assert {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in tables} == before
        for table in ("tasks", "task_revisions"):
            assert connection.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE points <> 1")).scalar_one() == 0
        for table in ("executions", "execution_revisions"):  # the snapshot is unknown, never back-filled
            assert connection.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE points IS NOT NULL")).scalar_one() == 0
    with predecessor.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(sa.text("UPDATE tasks SET points = -1"))


def test_a_failing_0007_rolls_back_to_a_working_0006(blank_engine) -> None:
    upgrade(blank_engine, "0006")
    with blank_engine.begin() as connection:  # makes 0007's create_table fail after its column changes
        connection.execute(sa.text("CREATE TABLE sync_operation_related_records (x INTEGER)"))
    with pytest.raises(sa.exc.DBAPIError):  # the table already exists
        upgrade(blank_engine)
    with blank_engine.connect() as connection:
        assert current_revision(connection) == "0006"
        columns = {column["name"] for column in sa.inspect(connection).get_columns("placements")}
        assert "removal_reason" not in columns and "task_category" not in columns


def test_0009_adds_recurrence_identity_without_touching_any_record(predecessor) -> None:
    """0008 -> 0009 (docs/recurrence.md): every row stays; templates get no guessed anchor (they need configuration)."""
    upgrade(predecessor, "0008")
    tables = ("tasks", "task_revisions", "placements", "executions", "change_log", "record_revisions",
              "sync_operations")
    with predecessor.connect() as connection:
        before = {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in tables}
        tasks_before = [tuple(row) for row in connection.execute(sa.text(
            "SELECT user_id, id, version, recurrence_frequency, recurrence_count FROM tasks ORDER BY user_id, id"))]
    upgrade(predecessor, "0009")
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0009"
        assert {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in tables} == before
        assert [tuple(row) for row in connection.execute(sa.text(
            "SELECT user_id, id, version, recurrence_frequency, recurrence_count FROM tasks ORDER BY user_id, id"
        ))] == tasks_before
        for table in ("tasks", "task_revisions"):
            assert connection.execute(sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE recurrence_start_date IS NOT NULL OR series_id IS NOT NULL "
                "OR occurrence_state IS NOT NULL OR series_predecessor_id IS NOT NULL")).scalar_one() == 0
    upgrade(predecessor)  # today's API reads the head schema (0011 added credential epochs to accounts)
    with client_for(predecessor) as client:  # the templates read back as needing configuration
        headers = login_headers(client, "alice@example.com")
        task = client.get(f"/tasks/{T1}", headers=headers).json()
        assert task["recurrence"]["frequency"] == "weekly" and task["recurrence"]["start_date"] is None
    with predecessor.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(sa.text("UPDATE tasks SET occurrence_state = 'skipped'"))  # none is an occurrence


def test_0009_downgrades_a_disposable_database_and_upgrades_again(predecessor) -> None:
    upgrade(predecessor)
    downgrade(predecessor, "0008")
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0008"
        columns = {column["name"] for column in sa.inspect(connection).get_columns("tasks")}
        assert "series_id" not in columns and "recurrence_start_date" not in columns
    upgrade(predecessor)
    with predecessor.connect() as connection:
        assert current_revision(connection) == head_revision()


def test_0010_adds_origin_intent_and_cancel_reasons_without_touching_any_record(predecessor) -> None:
    """0009 -> 0010 (docs/execution-rescheduling.md): every row stays; origins and reasons are unknown, not guessed."""
    upgrade(predecessor, "0009")
    tables = ("placements", "placement_revisions", "executions", "execution_revisions", "change_log",
              "record_revisions", "sync_operations")
    with predecessor.connect() as connection:
        before = {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in tables}
        placements_before = [tuple(row) for row in connection.execute(sa.text(
            "SELECT user_id, id, version, planned_start, deleted_at FROM placements ORDER BY user_id, id"))]
    upgrade(predecessor, "0010")
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0010"
        assert {table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in tables} == before
        assert [tuple(row) for row in connection.execute(sa.text(
            "SELECT user_id, id, version, planned_start, deleted_at FROM placements ORDER BY user_id, id"
        ))] == placements_before
        for table in ("placements", "placement_revisions"):
            assert connection.execute(sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE origin IS NOT NULL OR preserved")).scalar_one() == 0
        for table in ("executions", "execution_revisions"):
            assert connection.execute(sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE cancel_reason IS NOT NULL")).scalar_one() == 0
    with predecessor.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(sa.text("UPDATE placements SET preserved = true"))  # intent needs a manual origin


def test_0010_downgrades_a_disposable_database_and_upgrades_again(predecessor) -> None:
    upgrade(predecessor)
    downgrade(predecessor, "0009")
    with predecessor.connect() as connection:
        assert current_revision(connection) == "0009"
        assert "preserved" not in {column["name"] for column in sa.inspect(connection).get_columns("placements")}
        assert "cancel_reason" not in {column["name"] for column in sa.inspect(connection).get_columns("executions")}
    upgrade(predecessor)
    with predecessor.connect() as connection:
        assert current_revision(connection) == head_revision()
