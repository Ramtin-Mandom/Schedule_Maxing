"""
backend/snapshots.py

Immutable record snapshots (backend/models.py: record_revisions and its
typed tables) and the typed sync outcomes that reference them.

    encode(user_id, entity_type, record) -> RecordRevision   a record, as the API returns it, as rows
    decode(revision) -> dict                                  that record again (API JSON)
    outcome_row(...) -> SyncOperation / outcome(row) -> dict  a recorded sync result, both ways
                                                              (with the snapshots of its related records)

A record is parsed with the same Out schema the API serializes with and
written with the same content mapping as its live table (ResourceSpec.assign
and backend/record_mapping.py), so decode(encode(record)) is the record in
the server's canonical form: instants in UTC -- exactly what a later GET
returns -- and a task deadline with its original offset. The change log and
every sync response are built from decode(), so a response, the change feed
and a later retry of the same op_id always agree.

The JSON shapes of the change feed and of sync results are produced here,
at the serialization boundary; storage is typed rows only. An error detail
this module does not know how to store is refused loudly (ValueError)
rather than dropped.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from backend import models
from backend.executions import ExecutionFields, ExecutionOut
from backend.resources import CRUD_RESOURCES

_SPECS = {spec.entity_type: spec for spec in CRUD_RESOURCES}
_OUT = {**{entity_type: spec.out_schema for entity_type, spec in _SPECS.items()}, "execution": ExecutionOut}
_TYPE_BY_KEYS = {frozenset(schema.model_fields): entity_type for entity_type, schema in _OUT.items()}
_EXECUTION_COLUMNS = tuple(name for name in ExecutionFields.model_fields if name not in ("sessions", "status"))

REVISION_MODELS: dict[str, type[models.RecordRevision]] = {
    "project": models.ProjectRevision,
    "task": models.TaskRevision,
    "fixed_block": models.FixedBlockRevision,
    "placement": models.PlacementRevision,
    "preference": models.PreferenceRevision,
    "schedule_generation": models.ScheduleGenerationRevision,
    "execution": models.ExecutionRevision,
}

#: Revisions written in the current transaction, by (entity_type, entity_id, version) -- reused by a sync
#: outcome that reports the same record instead of storing a second copy.
KnownRevisions = dict[tuple[str, uuid.UUID, int], models.RecordRevision]


def entity_type_of(record: dict) -> str:
    """The entity type of an API record, from its exact field set (every type's set is distinct)."""
    try:
        return _TYPE_BY_KEYS[frozenset(record)]
    except KeyError:
        raise ValueError("the snapshot is not a record of a known type") from None


# -----------------------------------------------------------------------------
# Record revisions
# -----------------------------------------------------------------------------


def encode(user_id: uuid.UUID, entity_type: str, record: dict) -> models.RecordRevision:
    """A new (unsaved) revision holding `record`, an API record of `entity_type`."""
    payload = _OUT[entity_type].model_validate(record)
    revision = REVISION_MODELS[entity_type](
        user_id=user_id, id=uuid.uuid4(), entity_id=payload.id, version=payload.version,
        created_at=payload.created_at, updated_at=payload.updated_at, deleted_at=payload.deleted_at,
    )
    if entity_type == "execution":
        for name in _EXECUTION_COLUMNS:
            setattr(revision, name, getattr(payload, name))
        revision.status = payload.status.value
        revision.session_rows = [
            models.ExecutionRevisionSession(position=position, started_at=work.started_at, ended_at=work.ended_at)
            for position, work in enumerate(payload.sessions)
        ]
    else:
        _SPECS[entity_type].assign(None, user_id, revision, payload)
    return revision


def decode(revision: models.RecordRevision) -> dict:
    """The API record a revision holds (reads only the revision and its loaded child rows)."""
    data = {
        "id": revision.entity_id, "version": revision.version, "created_at": revision.created_at,
        "updated_at": revision.updated_at, "deleted_at": revision.deleted_at,
    }
    if revision.entity_type == "execution":
        data.update({name: getattr(revision, name) for name in _EXECUTION_COLUMNS}, status=revision.status)
        data["sessions"] = [{"started_at": work.started_at, "ended_at": work.ended_at} for work in revision.session_rows]
        return ExecutionOut.model_validate(data).model_dump(mode="json")
    spec = _SPECS[revision.entity_type]
    data.update(spec.content(None, revision.user_id, revision))
    return spec.out_schema.model_validate(data).model_dump(mode="json")


def _snapshot(user_id: uuid.UUID, record: dict, known: KnownRevisions) -> models.RecordRevision:
    entity_type = entity_type_of(record)
    existing = known.get((entity_type, uuid.UUID(record["id"]), record["version"]))
    if existing is not None and decode(existing) == record:
        return existing
    return encode(user_id, entity_type, record)


# -----------------------------------------------------------------------------
# Sync outcomes
# -----------------------------------------------------------------------------


def outcome_row(
    user_id: uuid.UUID, op_id: uuid.UUID, request_hash: str, recorded_at: datetime, result: dict,
    known: KnownRevisions,
) -> models.SyncOperation:
    """A new (unsaved) sync_operations row recording `result` ({op_id, status, record [, related] | error})."""
    status = result["status"]
    expected = {"op_id", "status", "record" if status == "applied" else "error"}
    if status == "applied" and "related" in result:
        expected.add("related")
    if set(result) != expected:
        raise ValueError(f"a {status} sync result must have exactly the fields {sorted(expected)}")
    row = models.SyncOperation(user_id=user_id, op_id=op_id, request_hash=request_hash, status=status,
                               recorded_at=recorded_at, error_problems_present=False)
    if status == "applied":
        row.record_revision = _snapshot(user_id, result["record"], known)
        related = []
        for position, item in enumerate(result.get("related", [])):
            if set(item) != {"entity_type", "record"} or entity_type_of(item["record"]) != item["entity_type"]:
                raise ValueError("a related record must be exactly {entity_type, record} of that type")
            related.append(models.SyncOperationRelatedRecord(
                position=position, revision=_snapshot(user_id, item["record"], known)))
        row.related_rows = related
        return row

    details = dict(result["error"])
    row.error_code, row.error_message = details.pop("code"), details.pop("message")
    if "supplied_version" in details or "current_version" in details:
        row.error_supplied_version = details.pop("supplied_version")
        row.error_current_version = details.pop("current_version")
        if row.error_current_version is None:
            raise ValueError("a version conflict must report the current version")
    if "current" in details:
        row.error_current_revision = _snapshot(user_id, details.pop("current"), known)
    if "conflicting" in details:
        row.error_conflicting_revision = _snapshot(user_id, details.pop("conflicting"), known)
    if "reason" in details:
        row.error_reason = details.pop("reason")
    if "failed_op_id" in details:
        row.error_failed_op_id = uuid.UUID(details.pop("failed_op_id"))
    if "problems" in details:
        row.error_problems_present = True
        row.problem_rows = [_problem(position, problem) for position, problem in enumerate(details.pop("problems"))]
    if details:
        raise ValueError(f"these sync error details cannot be stored: {', '.join(sorted(details))}")
    return row


def _problem(position: int, problem: dict) -> models.SyncOperationProblem:
    if set(problem) != {"location", "message"}:
        raise ValueError("a validation problem must have exactly a location and a message")
    return models.SyncOperationProblem(
        position=position, message=problem["message"],
        location_rows=[models.SyncOperationProblemLocation(position=index, part=part)
                       for index, part in enumerate(problem["location"])],
    )


def outcome(row: models.SyncOperation) -> dict:
    """The sync result a sync_operations row records, exactly as push returns it."""
    result: dict = {"op_id": str(row.op_id), "status": row.status}
    if row.status == "applied":
        result["record"] = decode(row.record_revision)
        if row.related_rows:
            result["related"] = [{"entity_type": item.revision.entity_type, "record": decode(item.revision)}
                                 for item in row.related_rows]
        return result
    error: dict = {"code": row.error_code, "message": row.error_message}
    if row.error_current_version is not None:
        error["supplied_version"] = row.error_supplied_version
        error["current_version"] = row.error_current_version
    if row.error_current_revision is not None:
        error["current"] = decode(row.error_current_revision)
    if row.error_conflicting_revision is not None:
        error["conflicting"] = decode(row.error_conflicting_revision)
    if row.error_reason is not None:
        error["reason"] = row.error_reason
    if row.error_problems_present:
        error["problems"] = [
            {"location": [part.part for part in problem.location_rows], "message": problem.message}
            for problem in row.problem_rows
        ]
    if row.error_failed_op_id is not None:
        error["failed_op_id"] = str(row.error_failed_op_id)
    result["error"] = error
    return result
