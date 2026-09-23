"""
app/sync/mapping.py

Translation between local records (the SQLite models) and the wire
representation of the backend (backend/resources.py, backend/executions.py).

Identity: planning records use their UUID on both sides. An execution's
wire id is its id when that is a UUID, otherwise the durable mapping in
execution_wire_ids, and the non-UUID local id travels as `legacy_id`
(docs/sync-contract.md section 3) -- a pulled execution with a legacy_id is
stored under that id again, never reminted.

Executions are an aggregate whose server copy changes only through
lifecycle actions and feedback. execution_changes() expresses the
difference between the last acknowledged server state (the shadow) and the
local aggregate as that sequence of actions (with the local session times
as `at`) followed by feedback; the engine sends them as one atomic group.
Local history that cannot be expressed that way (sessions that differ from
the server's) is reported as DivergedHistory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.execution.errors import ExecutionNotFoundError
from app.execution.models import TERMINAL_STATUSES, ExecutionStatus, TaskExecution
from app.execution.repository import ExecutionRepository
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import PreferenceOverrides, PreferenceRecord, PreferenceScope
from app.planning.provenance import GenerationRecord
from app.planning.repository import PlanningRepository

#: Creates/updates are sent in this order (a record after what it references); deletes in reverse.
ENTITY_ORDER = ("project", "task", "fixed_block", "placement", "preference", "schedule_generation", "execution")

_TASK_FIELDS = (
    "project_id", "name", "category", "tags", "estimated_duration_minutes", "priority", "required", "required_date",
    "preferred_dates", "preferred_time_window", "dependency_ids", "deadline", "recurrence",
)
_BLOCK_FIELDS = ("label", "category", "planned_date", "timezone", "planned_start", "planned_end")
_PLACEMENT_FIELDS = ("task_id", "planned_date", "timezone", "planned_start", "planned_end", "score",
                     "optimization_metadata")
_GENERATION_FIELDS = (
    "planned_date", "timezone", "engine_mode", "range_start", "range_end", "range_scope", "allocation_id",
    "fingerprint", "fingerprint_version", "placements_digest", "placement_count", "unscheduled_count",
    "total_score", "generated_at",
)
_EXECUTION_FIELDS = (
    "task_name", "category", "tag", "planned_date", "planned_start", "planned_end", "planned_duration", "priority",
    "actual_active_duration_minutes", "duration_variance_minutes", "start_delay_minutes", "focus_rating",
    "energy_rating", "interruption_count", "note", "canonical_planned_date", "canonical_timezone",
    "canonical_planned_start", "canonical_planned_end", "actual_first_start_at", "actual_final_end_at",
)
_FEEDBACK_FIELDS = ("focus_rating", "energy_rating", "interruption_count", "note")
_VERBS = {ExecutionStatus.COMPLETED: "complete", ExecutionStatus.SKIPPED: "skip", ExecutionStatus.CANCELLED: "cancel"}


class DivergedHistory(Exception):
    """The local execution's sessions do not extend the server's; it cannot be expressed as actions."""


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class LocalRecord:
    entity_type: str
    local_id: str
    wire_id: str
    owner: str | None
    deleted: bool
    #: The record's content in wire form (what a create/update sends).
    payload: dict[str, Any]
    model: Any
    sessions: list[tuple[str, str | None]] | None = None


def _dump(model, fields) -> dict:
    return model.model_dump(mode="json", include=set(fields))


class LocalRecords:
    """Reads local records (tombstones included) in wire form, and writes pulled ones."""

    def __init__(self, planning: PlanningRepository, executions: ExecutionRepository) -> None:
        self.planning = planning
        self.executions = executions

    # -- reading ----------------------------------------------------------

    def read(self, entity_type: str, local_id: str) -> LocalRecord | None:
        if entity_type == "execution":
            return self._read_execution(local_id)
        record_id = uuid.UUID(local_id)
        model, payload = None, None
        if entity_type == "project":
            model = self.planning.get_project(record_id, include_deleted=True)
            payload = model and {"name": model.name, "description": model.description}
        elif entity_type == "task":
            model = self.planning.get_task(record_id, include_deleted=True)
            payload = model and _dump(model, _TASK_FIELDS)
        elif entity_type == "fixed_block":
            model = self.planning.get_fixed_blocks([record_id], include_deleted=True).get(record_id)
            payload = model and _dump(model, _BLOCK_FIELDS)
        elif entity_type == "placement":
            model = self.planning.get_placements([record_id], include_deleted=True).get(record_id)
            payload = model and _dump(model, _PLACEMENT_FIELDS)
        elif entity_type == "preference":
            model = self.planning.get_preference_by_id(record_id, include_deleted=True)
            payload = model and {
                "scope": model.scope.value, "date": model.date.isoformat() if model.date else None,
                "overrides": model.overrides.model_dump(mode="json"),
            }
        elif entity_type == "schedule_generation":
            model = self.planning.get_generation_by_id(record_id, include_deleted=True)
            payload = model and _dump(model, _GENERATION_FIELDS)
        if model is None:
            return None
        return LocalRecord(entity_type, local_id, local_id, str(model.user_id) if model.user_id else None,
                           model.deleted_at is not None, payload, model)

    def _read_execution(self, local_id: str) -> LocalRecord | None:
        try:
            execution = self.executions.get_execution(local_id, include_deleted=True)
        except ExecutionNotFoundError:
            return None
        sessions = [(s.started_at, s.ended_at) for s in self.executions.list_sessions(local_id)]
        payload = {name: value for name, value in execution.model_dump(mode="json").items() if name in _EXECUTION_FIELDS}
        payload.update(
            task_id=str(execution.task_id) if execution.task_id else None,
            scheduled_task_id=str(execution.scheduled_task_id) if execution.scheduled_task_id else None,
            status=execution.status.value,
            sessions=[{"started_at": started, "ended_at": ended} for started, ended in sessions],
        )
        if not is_uuid(local_id):
            payload["legacy_id"] = local_id
        return LocalRecord("execution", local_id, str(self.executions.wire_id(local_id)),
                           str(execution.user_id) if execution.user_id else None, execution.deleted_at is not None,
                           payload, execution, sessions)

    def links_resolve(self, record: LocalRecord) -> bool:
        """Whether an execution's task/placement are live local records (else it is uploaded as history)."""
        execution = record.model
        if execution.task_id is None:
            return True
        if self.planning.get_task(execution.task_id) is None:
            return False
        return execution.scheduled_task_id is None or bool(self.planning.get_placements([execution.scheduled_task_id]))

    # -- writing pulled records ------------------------------------------------

    def local_id_for(self, entity_type: str, record: dict) -> str:
        if entity_type != "execution":
            return record["id"]
        return record.get("legacy_id") or self.executions.local_id_for_wire(uuid.UUID(record["id"])) or record["id"]

    def store(self, entity_type: str, record: dict, owner: str, local_version: int) -> None:
        meta = {
            "id": record["id"], "user_id": owner, "version": local_version, "created_at": record["created_at"],
            "updated_at": record["updated_at"], "deleted_at": record["deleted_at"],
        }
        if entity_type == "execution":
            self._store_execution(record, owner, local_version)
            return
        if entity_type == "project":
            model = Project.model_validate({**meta, "name": record["name"], "description": record["description"]})
        elif entity_type == "task":
            model = Task.model_validate({**meta, **{name: record[name] for name in _TASK_FIELDS}})
        elif entity_type == "fixed_block":
            model = FixedBlock.model_validate({**meta, **{name: record[name] for name in _BLOCK_FIELDS}})
        elif entity_type == "placement":
            model = ScheduledTask.model_validate({**meta, **{name: record[name] for name in _PLACEMENT_FIELDS}})
        elif entity_type == "preference":
            model = PreferenceRecord.model_validate({
                **meta, "scope": PreferenceScope(record["scope"]), "date": record["date"],
                "overrides": PreferenceOverrides.model_validate(record["overrides"]),
            })
        else:
            model = GenerationRecord.model_validate({**meta, **{name: record[name] for name in _GENERATION_FIELDS}})
        self.planning.store_synced(entity_type, model)

    def _store_execution(self, record: dict, owner: str, local_version: int) -> None:
        local_id = self.local_id_for("execution", record)
        execution = TaskExecution.model_validate({
            **{name: record[name] for name in _EXECUTION_FIELDS},
            "id": local_id, "user_id": owner, "task_id": record["task_id"],
            "scheduled_task_id": record["scheduled_task_id"], "status": record["status"],
            "created_at": _local_text(record["created_at"]), "updated_at": _local_text(record["updated_at"]),
            "version": local_version, "deleted_at": _local_text(record["deleted_at"]),
        })
        sessions = [(_local_text(s["started_at"]), _local_text(s["ended_at"])) for s in record["sessions"]]
        self.executions.store_synced(execution, sessions)
        if not is_uuid(local_id):
            self.executions.set_wire_id(local_id, uuid.UUID(record["id"]))


# -----------------------------------------------------------------------------
# Execution aggregate -> server actions
# -----------------------------------------------------------------------------


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_text(value: str | None) -> str | None:
    """
    A server timestamp ("...Z") in the local text form ("...+00:00"). Execution
    timestamps are stored as ISO text locally and read with
    datetime.fromisoformat, which on Python 3.10 does not accept "Z".
    """
    return _instant(value).isoformat() if value is not None else None


def execution_changes(shadow: dict, local: LocalRecord) -> list[tuple[str, str | None, dict]]:
    """
    (kind, action, payload) operations that turn the server aggregate `shadow`
    into the local one: lifecycle actions at the local session times, then
    feedback. Empty when nothing the server tracks has changed.
    """
    remote_sessions = shadow["sessions"]
    local_sessions = local.sessions or []
    if len(local_sessions) < len(remote_sessions):
        raise DivergedHistory("the server has sessions this device does not")
    for remote, (started, _) in zip(remote_sessions, local_sessions):
        if _instant(remote["started_at"]) != _instant(started):
            raise DivergedHistory("a session differs from the server's")

    execution: TaskExecution = local.model
    status = ExecutionStatus(shadow["status"])
    final = execution.actual_final_end_at
    operations: list[tuple[str, str | None, dict]] = []

    def close(at: str, is_last: bool) -> None:
        nonlocal status
        ends_execution = is_last and execution.status in TERMINAL_STATUSES and (
            final is None or abs((final - _instant(at)).total_seconds()) < 1
        )
        action = _VERBS[execution.status] if ends_execution else "pause"
        operations.append(("action", action, {"at": at}))
        status = execution.status if ends_execution else ExecutionStatus.PAUSED

    for index, (started, ended) in enumerate(local_sessions):
        is_last = index == len(local_sessions) - 1
        if index < len(remote_sessions):
            if remote_sessions[index]["ended_at"] is None and ended is not None:
                close(ended, is_last)
            continue
        action = "start" if status == ExecutionStatus.SCHEDULED else "resume"
        operations.append(("action", action, {"at": started}))
        status = ExecutionStatus.IN_PROGRESS
        if ended is not None:
            close(ended, is_last)

    if execution.status in TERMINAL_STATUSES and status != execution.status:
        at = final.isoformat() if final is not None else None
        operations.append(("action", _VERBS[execution.status], {"at": at} if at else {}))
    elif execution.status != status and execution.status not in TERMINAL_STATUSES:
        raise DivergedHistory(f"cannot move from {status.value} to {execution.status.value} with the recorded sessions")

    feedback = {name: local.payload[name] for name in _FEEDBACK_FIELDS
                if local.payload[name] is not None and local.payload[name] != shadow.get(name)}
    if feedback:
        operations.append(("feedback", None, feedback))
    return operations
