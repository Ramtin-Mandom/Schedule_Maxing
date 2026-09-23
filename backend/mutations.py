"""
backend/mutations.py

The one server mutation path. Every accepted change to a user's records --
REST CRUD today, batch synchronization later -- goes through a Mutator, which
    1. serializes the user's mutations (the change-log lock, below),
    2. checks the caller's precondition (base_version) against the stored
       record and refuses stale or tombstoned targets with a 409 carrying the
       current record/tombstone,
    3. writes the change with server-authoritative UTC audit fields and a
       server version (1 on create, +1 per accepted mutation; a no-op change
       is accepted without a new version),
    4. appends one change_log entry per changed record -- tombstones and every
       record touched by a compound change (e.g. a task delete that tombstones
       its placements) included -- in the same transaction.

Change ordering (for incremental pull, docs/backend.md): each user has a
counter, users.change_seq. A mutation begins with
`UPDATE users SET change_seq = change_seq WHERE id = :user`, which takes the
user's row lock (PostgreSQL) / the database write lock (SQLite) and holds it
until commit or rollback. Sequence numbers are then allocated from the
counter inside that transaction and the counter is written back before
commit. Consequently, for one user:
    - sequence numbers are assigned in commit order and without gaps: a
      second transaction cannot allocate a number until the first has
      committed (and sees its counter) or rolled back (and its numbers are
      reused); there is never an allocated-but-uncommitted number below a
      committed one;
    - a reader that has seen every entry up to seq N can resume with
      "seq > N" and can never skip a change that commits later.
Global order across users is not needed (every feed is per user), and
updated_at is never used for ordering.

Optimistic concurrency is enforced twice: the explicit version comparison
under the lock above, and SQLAlchemy's version_id_col, which adds
`AND version = <loaded version>` to every UPDATE (a StaleDataError there is
reported as a conflict too).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.execution.lifecycle import TRANSITIONS, compute_active_duration_minutes, compute_start_delay_minutes
from app.execution.models import ExecutionStatus
from app.execution.models import WorkSession as CanonicalSession
from app.planning.time import elapsed_minutes
from backend import models
from backend.errors import ApiError, invalid_reference, not_found, version_conflict
from backend.executions import EXECUTIONS, ActionIn, ExecutionCreate, FeedbackIn
from backend.resources import ResourceSpec, live

Clock = Callable[[], datetime]

#: How far in the future a client-reported action time may be (clock skew).
MAX_CLIENT_CLOCK_SKEW = timedelta(minutes=5)


class Mutator:
    def __init__(self, session: Session, user_id: uuid.UUID, clock: Clock) -> None:
        self.session = session
        self.user_id = user_id
        self.now = clock()
        self._seq: int | None = None

    # ------------------------------------------------------------------
    # Change log
    # ------------------------------------------------------------------

    def _lock(self) -> None:
        """Take the user's change-log lock and read the counter (see the module docstring)."""
        self.session.execute(
            update(models.User).where(models.User.id == self.user_id).values(change_seq=models.User.change_seq)
        )
        self._seq = self.session.scalar(select(models.User.change_seq).where(models.User.id == self.user_id))
        if self._seq is None:
            raise ApiError(401, "unauthenticated", "The account no longer exists.")

    def log(self, entity_type: str, record: dict, operation: str) -> None:
        self._seq += 1
        self.session.add(models.ChangeLogEntry(
            user_id=self.user_id, seq=self._seq, entity_type=entity_type, entity_id=uuid.UUID(record["id"]),
            operation=operation, version=record["version"], recorded_at=self.now, payload=record,
        ))

    def _finish(self) -> None:
        self.session.execute(
            update(models.User).where(models.User.id == self.user_id).values(change_seq=self._seq)
        )

    # ------------------------------------------------------------------
    # Generic records
    # ------------------------------------------------------------------

    def _load(self, spec_model, record_id: uuid.UUID, label: str):
        row = self.session.get(spec_model, (self.user_id, record_id), populate_existing=True)
        if row is None:
            raise not_found(label)
        return row

    def create(self, spec: ResourceSpec, payload) -> dict:
        record_id = payload.id or uuid.uuid4()
        existing = self.session.get(spec.model, (self.user_id, record_id))
        if existing is not None:
            raise ApiError(409, "already_exists", f"This {spec.label} already exists.",
                           current=spec.serialize(self.session, self.user_id, existing))
        spec.validate(self.session, self.user_id, payload, None)
        row = spec.model(user_id=self.user_id, id=record_id, created_at=self.now, updated_at=self.now, version=1)
        self.session.add(row)
        spec.assign(self.session, self.user_id, row, payload)
        self._flush(spec, row)
        record = spec.serialize(self.session, self.user_id, row)
        self.log(spec.entity_type, record, "upsert")
        return record

    def update(self, spec: ResourceSpec, record_id: uuid.UUID, payload) -> dict:
        row = self._load(spec.model, record_id, spec.label)
        self._check_precondition(spec, row, payload.base_version)
        spec.validate(self.session, self.user_id, payload, row)
        before = spec.content(self.session, self.user_id, row)
        spec.assign(self.session, self.user_id, row, payload)
        self.session.flush()
        if spec.content(self.session, self.user_id, row) == before:
            return spec.serialize(self.session, self.user_id, row)  # accepted, nothing changed: no new version
        row.updated_at, row.version = self.now, row.version + 1
        self._flush(spec, row)
        record = spec.serialize(self.session, self.user_id, row)
        self.log(spec.entity_type, record, "upsert")
        return record

    def delete(self, spec: ResourceSpec, record_id: uuid.UUID, base_version: int) -> dict:
        row = self._load(spec.model, record_id, spec.label)
        self._check_precondition(spec, row, base_version)
        spec.before_delete(self, row)
        return self.tombstone(spec, row)

    def tombstone(self, spec: ResourceSpec, row) -> dict:
        """Soft-delete one live row (also used for cascades) and log it."""
        row.deleted_at = row.updated_at = self.now
        row.version += 1
        self._flush(spec, row)
        record = spec.serialize(self.session, self.user_id, row)
        self.log(spec.entity_type, record, "delete")
        return record

    def _check_precondition(self, spec, row, base_version: int) -> None:
        if row.deleted_at is not None or row.version != base_version:
            raise version_conflict(spec.label, base_version, spec.serialize(self.session, self.user_id, row))

    def _flush(self, spec, row) -> None:
        # Rolling back is the caller's job (the request transaction or a sync savepoint), never done here.
        try:
            self.session.flush()
        except StaleDataError:
            # Unreachable while the user's lock is held; kept as a database-level safety net.
            raise ApiError(409, "version_conflict", f"The {spec.label} was changed concurrently.") from None
        except IntegrityError as error:
            raise ApiError(409, "constraint_violation", f"The {spec.label} conflicts with existing data.") from error

    # ------------------------------------------------------------------
    # Executions (an aggregate: the execution and its work sessions)
    # ------------------------------------------------------------------

    def create_execution(self, payload: ExecutionCreate) -> dict:
        record_id = payload.id or uuid.uuid4()
        existing = self.session.get(models.Execution, (self.user_id, record_id))
        if existing is not None:
            raise ApiError(409, "already_exists", "This execution already exists.",
                           current=EXECUTIONS.serialize(self.session, self.user_id, existing))
        if payload.legacy_id is not None and self.session.scalars(select(models.Execution.id).where(
            models.Execution.user_id == self.user_id, models.Execution.legacy_id == payload.legacy_id
        )).first() is not None:
            raise ApiError(409, "already_exists", "An execution with this legacy id already exists.")
        if payload.scheduled_task_id is not None and self.session.scalars(select(models.Execution.id).where(
            models.Execution.user_id == self.user_id, models.Execution.scheduled_task_id == payload.scheduled_task_id
        )).first() is not None:
            raise ApiError(409, "already_exists", "This placement already has an execution.")
        if not payload.historical_reference:
            self._check_execution_links(payload)

        fields = payload.model_dump(exclude={"id", "sessions", "status"})
        row = models.Execution(
            user_id=self.user_id, id=record_id, created_at=self.now, updated_at=self.now, version=1,
            status=payload.status.value, **fields,
        )
        self.session.add(row)
        self.session.flush()
        for position, work in enumerate(payload.sessions):
            self.session.add(models.WorkSession(
                user_id=self.user_id, execution_id=record_id, position=position,
                started_at=work.started_at, ended_at=work.ended_at,
            ))
        self._flush(EXECUTIONS, row)
        record = EXECUTIONS.serialize(self.session, self.user_id, row)
        self.log(EXECUTIONS.entity_type, record, "upsert")
        return record

    def _check_execution_links(self, payload: ExecutionCreate) -> None:
        """A new (non-historical) execution must link to the user's own live task and placement."""
        if payload.task_id is not None and live(self.session, models.Task, self.user_id, payload.task_id) is None:
            raise invalid_reference("task_id does not name one of your tasks.")
        if payload.scheduled_task_id is not None:
            placement = live(self.session, models.Placement, self.user_id, payload.scheduled_task_id)
            if placement is None:
                raise invalid_reference("scheduled_task_id does not name one of your placements.")
            if placement.task_id != payload.task_id:
                raise invalid_reference("The placement belongs to a different task.")

    def execution_action(self, record_id: uuid.UUID, action: str, payload: ActionIn) -> dict:
        row = self._load(models.Execution, record_id, "execution")
        self._check_precondition(EXECUTIONS, row, payload.base_version)
        allowed, target = TRANSITIONS[action]
        if ExecutionStatus(row.status) not in allowed:
            raise ApiError(409, "invalid_transition", f"Cannot {action} an execution that is {row.status}.",
                           current=EXECUTIONS.serialize(self.session, self.user_id, row))

        sessions = EXECUTIONS.sessions(self.session, self.user_id, row.id)
        at = payload.at or self.now
        boundaries = [work.ended_at or work.started_at for work in sessions]
        if boundaries and at < max(boundaries):
            raise ApiError(422, "validation_error", "The action time is before the execution's last recorded session.")
        if at > self.now + MAX_CLIENT_CLOCK_SKEW:
            raise ApiError(422, "validation_error", "The action time is in the future.")
        open_session = next((work for work in sessions if work.ended_at is None), None)

        if action in ("start", "resume"):
            self.session.add(models.WorkSession(
                user_id=self.user_id, execution_id=row.id, position=len(sessions), started_at=at, ended_at=None,
            ))
            if row.actual_first_start_at is None:
                row.actual_first_start_at = at
        elif open_session is not None:
            open_session.ended_at = at
        row.status = target.value
        self.session.flush()

        if action == "complete":
            sessions = EXECUTIONS.sessions(self.session, self.user_id, row.id)
            canonical = [
                CanonicalSession(execution_id=str(row.id), started_at=work.started_at.isoformat(),
                                 ended_at=work.ended_at.isoformat() if work.ended_at else None)
                for work in sessions
            ]
            row.actual_active_duration_minutes = compute_active_duration_minutes(canonical)
            row.duration_variance_minutes = round(row.actual_active_duration_minutes - row.planned_duration, 2)
            if row.canonical_planned_start is not None and row.actual_first_start_at is not None:
                row.start_delay_minutes = round(elapsed_minutes(row.canonical_planned_start, row.actual_first_start_at), 2)
            elif row.planned_start is not None and canonical:
                row.start_delay_minutes = compute_start_delay_minutes(canonical[0].started_at, row.planned_start)
        if action in ("complete", "skip", "cancel"):
            row.actual_final_end_at = at
        return self._commit_execution(row)

    def execution_feedback(self, record_id: uuid.UUID, payload: FeedbackIn) -> dict:
        row = self._load(models.Execution, record_id, "execution")
        self._check_precondition(EXECUTIONS, row, payload.base_version)
        changes = payload.model_dump(exclude={"base_version"}, exclude_unset=True)
        changes = {name: value for name, value in changes.items() if value is not None}
        if all(getattr(row, name) == value for name, value in changes.items()):
            return EXECUTIONS.serialize(self.session, self.user_id, row)
        for name, value in changes.items():
            setattr(row, name, value)
        return self._commit_execution(row)

    def _commit_execution(self, row) -> dict:
        row.updated_at, row.version = self.now, row.version + 1
        self._flush(EXECUTIONS, row)
        record = EXECUTIONS.serialize(self.session, self.user_id, row)
        self.log(EXECUTIONS.entity_type, record, "upsert")
        return record

    def delete_execution(self, record_id: uuid.UUID, base_version: int) -> dict:
        """Tombstone one execution; its work sessions stay with it (history)."""
        row = self._load(models.Execution, record_id, "execution")
        self._check_precondition(EXECUTIONS, row, base_version)
        return self.tombstone(EXECUTIONS, row)


@contextmanager
def mutation(session: Session, user_id: uuid.UUID, clock: Clock) -> Iterator[Mutator]:
    """
    One atomic, serialized mutation of a user's records: everything written
    through the yielded Mutator, and its change-log entries, commit together
    or not at all.
    """
    mutator = Mutator(session, user_id, clock)
    try:
        mutator._lock()
        yield mutator
        mutator._finish()
        session.commit()
    except BaseException:
        session.rollback()
        raise
