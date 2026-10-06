"""
backend/sync.py

Batch synchronization (docs/sync-protocol.md).

POST /sync/push {"operations": [...]} applies up to MAX_PUSH_OPERATIONS
operations, in order, through the same Mutator as the REST endpoints -- the
same user scoping, preconditions, relationship rules, version rules and
change-log writes -- and returns one result per operation, in order:

    {"op_id", "status": "applied" | "conflict" | "rejected", "record"?, "related"?, "error"?}

    applied   the operation was accepted; `record` is the resulting server record
              (and `related` [{"entity_type", "record"}] the other records it
              changed, for an operation that changes several -- a reschedule)
    conflict  a precondition or uniqueness conflict (HTTP 409 on the REST API):
              stale base_version, tombstoned target, already exists, in use,
              invalid transition; `error.current` carries the caller's current
              record/tombstone when there is one
    rejected  invalid content or reference (HTTP 404/422 on the REST API)

Idempotency: every operation has a client-chosen op_id. Its outcome is
recorded (sync_operations) -- for an applied operation in the same
transaction as the mutation itself, so a lost response after commit is
answered from the record on retry, with no second write, record, session or
version increment. A rejected/conflicting outcome is recorded too, so
retries are stable. Outcomes are typed rows that reference immutable record
snapshots (backend/snapshots.py), so a retry answers exactly the same even
after the record was edited later; the response itself is built from what
was recorded. The lookup happens while the user's change-log lock is held,
so two concurrent pushes of the same op_id cannot both apply it.
Reusing an op_id for a different operation (a different request hash) is
answered with status "rejected", code "op_id_reused", and changes nothing.

Atomic groups and partial batches: operations sharing a `group` id must be
contiguous; a group is applied all-or-nothing (one savepoint) -- if any of its
operations fails, none is applied and every operation in it reports the
failure (the failing one its own error, the others "group_failed").
Operations outside groups are independent: a failure affects only that
operation, earlier and later ones still apply.

Placements (Milestone 5, docs/execution-rescheduling.md):
    - action "reschedule" on a placement moves it atomically in one unit, with
      base_version = the moved placement's version and a payload naming the
      replacement (replacement_id, planned_date, timezone, planned_start,
      planned_end, optional task_category and `at`, the time of the move on
      the device). It runs the shared workflow.reschedule_placement against
      the server's records under the user's lock: the placement's tombstone
      is `record`; the replacement and the cancelled never-started execution
      (if any) are `related`. A stale, moved, history-protected or invalid
      move is a conflict carrying the placement as stored now.
    - a placement delete may carry {"removal_reason", "superseded_by_id"}
      (why the device removed it); without a payload the reason is unknown.
    - a placement create carrying removal_reason ("rescheduled" or
      "regenerated") and superseded_by_id uploads *history*: a plan the device
      made and then moved or re-placed before it ever reached the server. It is
      stored as a tombstone at once (Mutator.create_placement_history) --
      nothing live is created or changed.

Recurring series (docs/recurrence.md):
    - occurrences travel as tasks with series_id/occurrence_slot (their id is
      derived from both). A create repeating an occurrence the server already
      has with the same content is answered "applied" with the stored record
      (two devices expanding the same slot converge); different content is a
      conflict. A create carrying a tombstone occurrence_state (skipped,
      deleted, superseded) stores a tombstone at once, so a slot suppressed
      offline stays reserved;
    - a task delete may carry {"occurrence_state"} (why the device removed
      the occurrence; default deleted);
    - a reschedule that moves an occurrence to another date re-dates the
      occurrence task in the same unit; it is then one of the `related`
      records.

Capability negotiation: GET /sync/capabilities names the protocol version
and features (SYNC_PROTOCOL_VERSION, SYNC_FEATURES). A client sends
recurrence data -- series anchors, occurrence identity, exception states --
only to a server that lists "recurrence_occurrences"; to an older server it
holds those operations instead of letting their fields be dropped. An older
client that omits the fields cannot erase them here (backend/resources.py
keeps omitted fields on update). Likewise "manual_placements": placement
origin and manual intent and execution cancel reasons
(docs/execution-rescheduling.md) are sent only to a server that lists it;
an older client's placement update without them keeps the stored values.

Pull is GET /changes (backend/api.py): the per-user, gap-free, commit-ordered
feed with an integer cursor.
"""

import hashlib
import json
import uuid
from typing import Annotated, Any, Literal

from datetime import date

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.planning.application import PlanningService
from app.planning.models import PlacementRemovalReason
from backend import models, snapshots
from backend.api import current_user_id, get_session
from backend.errors import ApiError
from backend.executions import ACTIONS, EXECUTIONS, ActionIn, ExecutionCreate, FeedbackIn
from backend.mutations import Mutator, mutation
from backend.planning_api import RescheduleIn, reschedule
from backend.planning_repository import ServerPlanningRepository
from backend.resources import (
    FIXED_BLOCKS,
    GENERATIONS,
    MAX_TAGS,
    PLACEMENTS,
    PREFERENCES,
    PROJECTS,
    TASK_TYPES,
    TASKS,
    Strict,
)

MAX_PUSH_OPERATIONS = 200

#: The synchronization protocol this server speaks, and its optional features (GET /sync/capabilities).
SYNC_PROTOCOL_VERSION = 2
#: "task_types": task-type records synchronize, tasks carry task_type_id and placements their planning snapshot
#: (docs/productivity-redesign-plan.md). A client holds its type records back from a server without it.
#: "project_details": a project carries its planned dates, completion and milestones.
SYNC_FEATURES = ("placement_reschedule", "recurrence_occurrences", "manual_placements", "scheduling_modes",
                 "task_types", "project_details")

SPECS = {spec.entity_type: spec
         for spec in (PROJECTS, TASK_TYPES, TASKS, FIXED_BLOCKS, PLACEMENTS, PREFERENCES, GENERATIONS)}
ENTITY_TYPES = (*SPECS, "execution")


class SyncOperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: uuid.UUID
    entity_type: Literal["project", "task_type", "task", "fixed_block", "placement", "preference",
                         "schedule_generation", "execution"]
    entity_id: uuid.UUID
    kind: Literal["create", "update", "delete", "action", "feedback"]
    base_version: int | None = Field(default=None, gt=0)
    action: Literal["start", "pause", "resume", "complete", "skip", "cancel", "reopen", "reschedule"] | None = None
    payload: dict[str, Any] | None = None
    group: uuid.UUID | None = None

    @model_validator(mode="after")
    def _shape(self):
        if (self.kind == "create") != (self.base_version is None):
            raise ValueError("base_version is required for every kind except create (and not allowed for create)")
        if self.kind in ("create", "update", "feedback") and self.payload is None:
            raise ValueError(f"a {self.kind} needs a payload")
        if self.kind == "feedback" and self.entity_type != "execution":
            raise ValueError("feedback applies to executions only")
        if (self.kind == "action") != (self.action is not None):
            raise ValueError("an action operation names exactly one action")
        if self.kind == "action" and (self.entity_type == "placement") != (self.action == "reschedule"):
            raise ValueError("a placement's only action is reschedule; the lifecycle actions apply to executions")
        if self.kind == "action" and self.entity_type not in ("execution", "placement"):
            raise ValueError("actions apply to executions and placements only")
        if self.kind == "action" and self.action == "reschedule" and self.payload is None:
            raise ValueError("a reschedule needs a payload")
        if self.entity_type == "execution" and self.kind == "update":
            raise ValueError("executions change through actions and feedback, not updates")
        return self

    def request_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"op_id"})
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[SyncOperationIn] = Field(min_length=1, max_length=MAX_PUSH_OPERATIONS)

    @model_validator(mode="after")
    def _contiguous_groups(self):
        seen, previous = set(), None
        for op in self.operations:
            if op.group is not None and op.group != previous and op.group in seen:
                raise ValueError("operations of one group must be contiguous")
            if op.group is not None:
                seen.add(op.group)
            previous = op.group
        if len({op.op_id for op in self.operations}) != len(self.operations):
            raise ValueError("an op_id may appear only once per request")
        return self


class OperationResult(BaseModel):
    op_id: uuid.UUID
    status: Literal["applied", "conflict", "rejected"]
    record: dict[str, Any] | None = None
    #: For an applied operation that changed several records: the others, as {"entity_type", "record"}.
    related: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None


class SyncRescheduleIn(Strict):
    """The payload of a placement's reschedule action (base_version is the operation's)."""

    replacement_id: uuid.UUID
    planned_date: date
    timezone: str
    planned_start: AwareDatetime
    planned_end: AwareDatetime
    task_category: str | None = Field(default=None, min_length=1, max_length=100)
    #: The rest of the replacement's planning snapshot as the device recorded it (omitted by an older client:
    #: the server then takes the task as it is stored here).
    task_name: str | None = Field(default=None, min_length=1, max_length=500)
    task_tags: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=MAX_TAGS)
    task_points: int | None = Field(default=None, ge=0)
    task_estimate_minutes: int | None = Field(default=None, gt=0)
    task_type_id: uuid.UUID | None = None
    task_type_label: str | None = Field(default=None, min_length=1, max_length=500)
    #: When the move happened on the device (the cancelled execution's end time); default: server time.
    at: AwareDatetime | None = None


class PlacementRemovalIn(Strict):
    """The optional payload of a placement delete: why the device removed it."""

    removal_reason: PlacementRemovalReason | None = None
    superseded_by_id: uuid.UUID | None = None


class TaskRemovalIn(Strict):
    """The optional payload of a task delete: why the device removed an occurrence."""

    occurrence_state: Literal["skipped", "deleted", "superseded"] | None = None


class PushOut(BaseModel):
    results: list[OperationResult]


class CapabilitiesOut(BaseModel):
    protocol_version: int
    features: list[str]
    max_push_operations: int


def _validated(schema: type[BaseModel], data: dict) -> BaseModel:
    try:
        return schema.model_validate(data)
    except ValidationError as error:
        problems = [{"location": [str(part) for part in item.get("loc", ())], "message": str(item.get("msg", ""))}
                    for item in error.errors()]
        raise ApiError(422, "validation_error", "The operation is not valid.", problems=problems) from None


Related = list[dict[str, Any]]


def _apply(mutator: Mutator, op: SyncOperationIn) -> tuple[dict, Related | None]:
    """The operation's resulting record, and the other records it changed (only a reschedule has any)."""
    payload = dict(op.payload or {})
    if op.entity_type == "execution":
        if op.kind == "create":
            return mutator.create_execution(_validated(ExecutionCreate, {**payload, "id": op.entity_id})), None
        if op.kind == "delete":
            return mutator.delete_execution(op.entity_id, op.base_version), None
        if op.kind == "action":
            if op.action not in ACTIONS:
                raise ApiError(422, "validation_error", "Unknown action.")
            action = _validated(ActionIn, {"base_version": op.base_version, **payload})
            return mutator.execution_action(op.entity_id, op.action, action), None
        return mutator.execution_feedback(
            op.entity_id, _validated(FeedbackIn, {**payload, "base_version": op.base_version})), None

    spec = SPECS[op.entity_type]
    if op.kind == "action":
        return _reschedule(mutator, op.entity_id, op.base_version, _validated(SyncRescheduleIn, payload))
    if op.kind == "create":
        created = _validated(spec.create_schema, {**payload, "id": op.entity_id})
        if spec is PLACEMENTS and (created.removal_reason is not None or created.superseded_by_id is not None):
            return mutator.create_placement_history(created), None
        return mutator.create(spec, created), None
    if op.kind == "update":
        return mutator.update(
            spec, op.entity_id, _validated(spec.update_schema, {**payload, "base_version": op.base_version})), None
    if op.kind == "delete":
        if spec is PLACEMENTS:
            removal = _validated(PlacementRemovalIn, payload)
            reason = removal.removal_reason.value if removal.removal_reason is not None else None
            return mutator.delete(spec, op.entity_id, op.base_version, removal_reason=reason,
                                  superseded_by_id=removal.superseded_by_id), None
        if spec is TASKS:
            removal = _validated(TaskRemovalIn, payload)
            return mutator.delete(spec, op.entity_id, op.base_version, occurrence_state=removal.occurrence_state), None
        return mutator.delete(spec, op.entity_id, op.base_version), None
    raise ApiError(422, "validation_error", f"{op.kind} does not apply to a {op.entity_type}.")


def _reschedule(
    mutator: Mutator, placement_id: uuid.UUID, base_version: int, payload: SyncRescheduleIn
) -> tuple[dict, Related]:
    """The shared reschedule workflow on the server's records, inside this push's mutation (one unit)."""
    now = mutator.now
    session = mutator.session
    service = PlanningService(
        ServerPlanningRepository(session, mutator.user_id, lambda: now, mutator=mutator), lambda: now
    )
    request = RescheduleIn(
        base_version=base_version, planned_date=payload.planned_date, timezone=payload.timezone,
        planned_start=payload.planned_start, planned_end=payload.planned_end, replacement_id=payload.replacement_id,
    )
    snapshot = payload.model_dump(include={"task_name", "task_tags", "task_points", "task_estimate_minutes",
                                           "task_type_id", "task_type_label"})
    result = reschedule(service, placement_id, request, at=payload.at or now, task_category=payload.task_category,
                        snapshot=snapshot)

    def placement(record_id: uuid.UUID) -> dict:
        return PLACEMENTS.serialize(session, mutator.user_id, session.get(models.Placement, (mutator.user_id, record_id)))

    related: Related = [{"entity_type": "placement", "record": placement(result.replacement.id)}]
    if result.updated_task is not None:  # an occurrence moved to another date: re-dated in the same unit
        task = session.get(models.Task, (mutator.user_id, result.updated_task.id))
        related.append({"entity_type": "task", "record": TASKS.serialize(session, mutator.user_id, task)})
    if result.cancelled_execution_id is not None:
        execution = session.get(models.Execution, (mutator.user_id, uuid.UUID(result.cancelled_execution_id)))
        related.append({"entity_type": "execution",
                        "record": EXECUTIONS.serialize(session, mutator.user_id, execution)})
    return placement(result.previous.id), related


def _units(operations: list[SyncOperationIn]) -> list[list[SyncOperationIn]]:
    units: list[list[SyncOperationIn]] = []
    for op in operations:
        if op.group is not None and units and units[-1][0].group == op.group:
            units[-1].append(op)
        else:
            units.append([op])
    return units


def _record(session: Session, mutator: Mutator, op: SyncOperationIn, result: dict) -> dict:
    """Record one outcome; returns the result as recorded (what every retry of the op_id answers)."""
    row = snapshots.outcome_row(mutator.user_id, op.op_id, op.request_hash(), mutator.now, result, mutator.revisions)
    session.add(row)
    return snapshots.outcome(row)


def _push_unit(session: Session, user_id: uuid.UUID, clock, unit: list[SyncOperationIn]) -> list[dict]:
    with mutation(session, user_id, clock) as mutator:
        stored = {
            row.op_id: row for row in session.scalars(select(models.SyncOperation).where(
                models.SyncOperation.user_id == user_id,
                models.SyncOperation.op_id.in_([op.op_id for op in unit]),
            ))
        }
        if stored:
            reused = [op for op in unit if op.op_id in stored and stored[op.op_id].request_hash != op.request_hash()]
            if reused or len(stored) != len(unit):
                # A different operation under a known op_id (or a group that does not match what was recorded).
                return [{"op_id": str(op.op_id), "status": "rejected", "error": {
                    "code": "op_id_reused", "message": "This op_id was already used for a different operation.",
                }} for op in unit]
            return [snapshots.outcome(stored[op.op_id]) for op in unit]  # a retry: the recorded outcome

        seq_before, revisions_before = mutator._seq, dict(mutator.revisions)
        results: list[dict] = []
        try:
            with session.begin_nested():
                for op in unit:
                    record, related = _apply(mutator, op)
                    result = {"op_id": str(op.op_id), "status": "applied", "record": record}
                    if related:
                        result["related"] = related
                    results.append(result)
        except ApiError as error:
            # The savepoint's change-log entries and their snapshots were rolled back with it.
            mutator._seq, mutator.revisions = seq_before, revisions_before
            failed_status = "conflict" if error.status == 409 else "rejected"
            failing = unit[len(results)]
            results = []
            for op in unit:
                if op is failing:
                    results.append({"op_id": str(op.op_id), "status": failed_status, "error": error.body()["error"]})
                else:
                    results.append({"op_id": str(op.op_id), "status": failed_status, "error": {
                        "code": "group_failed", "message": f"Operation {failing.op_id} of this group failed.",
                        "failed_op_id": str(failing.op_id),
                    }})
        return [_record(session, mutator, op, result) for op, result in zip(unit, results)]


sync = APIRouter(prefix="/sync", tags=["sync"])


@sync.get("/capabilities", response_model=CapabilitiesOut, summary="The sync protocol version and features.")
def capabilities(_user_id: uuid.UUID = Depends(current_user_id)) -> dict:
    return {"protocol_version": SYNC_PROTOCOL_VERSION, "features": list(SYNC_FEATURES),
            "max_push_operations": MAX_PUSH_OPERATIONS}


@sync.post("/push", response_model=PushOut, summary="Apply a batch of client operations idempotently.")
def push(
    payload: PushIn,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    results: list[dict] = []
    for unit in _units(payload.operations):
        results.extend(_push_unit(session, user_id, request.app.state.clock, unit))
    return {"results": results}
