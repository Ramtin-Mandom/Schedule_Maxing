"""
backend/sync.py

Batch synchronization (docs/sync-protocol.md).

POST /sync/push {"operations": [...]} applies up to MAX_PUSH_OPERATIONS
operations, in order, through the same Mutator as the REST endpoints -- the
same user scoping, preconditions, relationship rules, version rules and
change-log writes -- and returns one result per operation, in order:

    {"op_id", "status": "applied" | "conflict" | "rejected", "record"?, "error"?}

    applied   the operation was accepted; `record` is the resulting server record
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
retries are stable. The lookup happens while the user's change-log lock is
held, so two concurrent pushes of the same op_id cannot both apply it.
Reusing an op_id for a different operation (a different request hash) is
answered with status "rejected", code "op_id_reused", and changes nothing.

Atomic groups and partial batches: operations sharing a `group` id must be
contiguous; a group is applied all-or-nothing (one savepoint) -- if any of its
operations fails, none is applied and every operation in it reports the
failure (the failing one its own error, the others "group_failed").
Operations outside groups are independent: a failure affects only that
operation, earlier and later ones still apply.

Pull is GET /changes (backend/api.py): the per-user, gap-free, commit-ordered
feed with an integer cursor.
"""

import hashlib
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import models
from backend.api import current_user_id, get_session
from backend.errors import ApiError
from backend.executions import ACTIONS, ActionIn, ExecutionCreate, FeedbackIn
from backend.mutations import Mutator, mutation
from backend.resources import FIXED_BLOCKS, GENERATIONS, PLACEMENTS, PREFERENCES, PROJECTS, TASKS

MAX_PUSH_OPERATIONS = 200

SPECS = {spec.entity_type: spec for spec in (PROJECTS, TASKS, FIXED_BLOCKS, PLACEMENTS, PREFERENCES, GENERATIONS)}
ENTITY_TYPES = (*SPECS, "execution")


class SyncOperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: uuid.UUID
    entity_type: Literal["project", "task", "fixed_block", "placement", "preference", "schedule_generation", "execution"]
    entity_id: uuid.UUID
    kind: Literal["create", "update", "delete", "action", "feedback"]
    base_version: int | None = Field(default=None, gt=0)
    action: Literal["start", "pause", "resume", "complete", "skip", "cancel"] | None = None
    payload: dict[str, Any] | None = None
    group: uuid.UUID | None = None

    @model_validator(mode="after")
    def _shape(self):
        if (self.kind == "create") != (self.base_version is None):
            raise ValueError("base_version is required for every kind except create (and not allowed for create)")
        if self.kind in ("create", "update", "feedback") and self.payload is None:
            raise ValueError(f"a {self.kind} needs a payload")
        if self.kind in ("action", "feedback") and self.entity_type != "execution":
            raise ValueError("actions and feedback apply to executions only")
        if (self.kind == "action") != (self.action is not None):
            raise ValueError("an action operation names exactly one action")
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
    error: dict[str, Any] | None = None


class PushOut(BaseModel):
    results: list[OperationResult]


def _validated(schema: type[BaseModel], data: dict) -> BaseModel:
    try:
        return schema.model_validate(data)
    except ValidationError as error:
        problems = [{"location": [str(part) for part in item.get("loc", ())], "message": str(item.get("msg", ""))}
                    for item in error.errors()]
        raise ApiError(422, "validation_error", "The operation is not valid.", problems=problems) from None


def _apply(mutator: Mutator, op: SyncOperationIn) -> dict:
    payload = dict(op.payload or {})
    if op.entity_type == "execution":
        if op.kind == "create":
            return mutator.create_execution(_validated(ExecutionCreate, {**payload, "id": op.entity_id}))
        if op.kind == "delete":
            return mutator.delete_execution(op.entity_id, op.base_version)
        if op.kind == "action":
            if op.action not in ACTIONS:
                raise ApiError(422, "validation_error", "Unknown action.")
            action = _validated(ActionIn, {"base_version": op.base_version, **payload})
            return mutator.execution_action(op.entity_id, op.action, action)
        return mutator.execution_feedback(op.entity_id, _validated(FeedbackIn, {**payload, "base_version": op.base_version}))

    spec = SPECS[op.entity_type]
    if op.kind == "create":
        return mutator.create(spec, _validated(spec.create_schema, {**payload, "id": op.entity_id}))
    if op.kind == "update":
        return mutator.update(spec, op.entity_id, _validated(spec.update_schema, {**payload, "base_version": op.base_version}))
    if op.kind == "delete":
        return mutator.delete(spec, op.entity_id, op.base_version)
    raise ApiError(422, "validation_error", f"{op.kind} does not apply to a {op.entity_type}.")


def _units(operations: list[SyncOperationIn]) -> list[list[SyncOperationIn]]:
    units: list[list[SyncOperationIn]] = []
    for op in operations:
        if op.group is not None and units and units[-1][0].group == op.group:
            units[-1].append(op)
        else:
            units.append([op])
    return units


def _record(session: Session, mutator: Mutator, op: SyncOperationIn, result: dict) -> None:
    session.add(models.SyncOperation(
        user_id=mutator.user_id, op_id=op.op_id, request_hash=op.request_hash(), status=result["status"],
        result=result, recorded_at=mutator.now,
    ))


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
            return [stored[op.op_id].result for op in unit]  # a retry: the recorded outcome, nothing reapplied

        seq_before = mutator._seq
        results: list[dict] = []
        try:
            with session.begin_nested():
                for op in unit:
                    record = _apply(mutator, op)
                    results.append({"op_id": str(op.op_id), "status": "applied", "record": record})
        except ApiError as error:
            mutator._seq = seq_before  # the savepoint's change-log entries were rolled back with it
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
        for op, result in zip(unit, results):
            _record(session, mutator, op, result)
        return results


sync = APIRouter(prefix="/sync", tags=["sync"])


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
