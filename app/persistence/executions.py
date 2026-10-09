"""
app/persistence/executions.py

Execution tracking over PostgreSQL for the direct desktop path.

DirectExecutionService offers the public API of
app.execution.service.ExecutionService (what ExecutionController uses), but
every mutation goes through the backend's own mutation path
(backend/mutations.py: create_execution, execution_action,
execution_feedback, delete_execution). That path applies the shared
transition table and completion metrics of app/execution/lifecycle.py and
the server's history validation (ordered, non-overlapping sessions; no
action time before the last session or in the future), under the user's
change-log lock, with server versions and one change-log entry per logical
mutation -- a status change and its work session commit together or not at
all. There is no second status machine here: this module only translates
between the domain objects and the server's records and errors.

Differences from the SQLite service, all deliberate:
    - ids are the server's UUIDs (TaskExecution.id is its string form);
      work-session ids are their 1-based position in the execution;
    - timestamps are the server clock's (the injected clock), UTC;
    - reset_all_history() tombstones every execution of the account (each
      a logged, versioned delete) -- the server never purges history rows.

DirectExecutionReader serves ProductivityService (list_executions and
list_sessions): one list_executions() reads the executions and all their
sessions in one unit of work, and list_sessions() answers from that
snapshot on the same thread, so a dashboard is consistent and costs two
queries rather than one per execution.

Every call is its own unit of work (see app/persistence/direct.py); results
are detached TaskExecution/WorkSession objects.
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.errors import (
    ExecutionDeletedError,
    ExecutionError,
    ExecutionLinkError,
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
    InvalidTransitionError,
)
from app.execution.lifecycle import TRANSITIONS, BulkOutcomeResult, OutcomeChangeError, TaskOutcome
from app.execution.models import ExecutionStatus, TaskExecution, WorkSession
from app.execution.service import build_canonical_execution, check_feedback
from app.models import ScheduledTask
from app.persistence import errors
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.planning.scope import OwnerScope
from backend import models
from backend.errors import ApiError
from backend.executions import SNAPSHOT_FIELDS as _SNAPSHOT_FIELDS
from backend.executions import ActionIn, ExecutionCreate, FeedbackIn
from backend.executions import to_task_execution as to_execution
from backend.executions import to_work_session as to_session
from backend.mutations import mutation
from backend.outcomes import OutcomeIn, set_placement_outcome, set_placements_outcome


def _execution_uuid(execution_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(execution_id))
    except ValueError:
        raise ExecutionNotFoundError(str(execution_id)) from None


def _sessions(session: Session, user_id: uuid.UUID, execution_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[WorkSession]]:
    grouped: dict[uuid.UUID, list[WorkSession]] = defaultdict(list)
    if execution_ids:
        for row in session.scalars(
            select(models.WorkSession)
            .where(models.WorkSession.user_id == user_id, models.WorkSession.execution_id.in_(execution_ids))
            .order_by(models.WorkSession.execution_id, models.WorkSession.position)
        ):
            grouped[row.execution_id].append(to_session(row))
    return grouped


def _live_rows(session: Session, user_id: uuid.UUID, status: ExecutionStatus | None) -> list[models.Execution]:
    query = select(models.Execution).where(models.Execution.user_id == user_id, models.Execution.deleted_at.is_(None))
    if status is not None:
        query = query.where(models.Execution.status == status.value)
    return list(session.scalars(query.order_by(models.Execution.created_at, models.Execution.id)))


class DirectExecutionService:
    def __init__(self, account) -> None:
        self._account = account

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    @property
    def owner_scope(self) -> OwnerScope:
        return self._account.scope

    def scoped(self, owner: OwnerScope) -> "DirectExecutionService":
        if owner != self.owner_scope:
            raise ExecutionLinkError("A direct execution service is bound to the signed-in account.")
        return self

    def _owner(self, user_id: uuid.UUID | None) -> uuid.UUID:
        if user_id is not None and user_id != self._account.user_id:
            raise ExecutionLinkError("An execution can only be created for the signed-in account.")
        return self._account.user_id

    # ------------------------------------------------------------------
    # Units of work
    # ------------------------------------------------------------------

    def _translate(self, execution_id: str | None, expected_version: int | None = None, action: str | None = None):
        def translate(error: ApiError) -> Exception:
            if error.status == 401:
                return errors.NotSignedInError()
            if error.status == 404:
                return ExecutionNotFoundError(str(execution_id))
            if error.code in ("version_conflict", "deleted"):
                return ExecutionVersionConflictError(
                    str(execution_id), expected_version=error.details.get("supplied_version", expected_version),
                    current_version=error.details.get("current_version"), deleted=error.code == "deleted",
                )
            if error.code == "invalid_transition" and action is not None:
                current = ExecutionStatus(error.details["current"]["status"])
                return InvalidTransitionError(str(execution_id), current, TRANSITIONS[action][1])
            if error.code == "invalid_transition":
                return OutcomeChangeError(error.message)
            if error.code == "invalid_reference":
                return ExecutionLinkError(error.message)
            if error.status == 409:
                return errors.StorageConflictError(error.message)
            return ExecutionError(error.message)

        return translate

    def _load(self, session: Session, execution_id: str, *, include_deleted: bool = False) -> models.Execution:
        row = session.get(models.Execution, (self._account.user_id, _execution_uuid(execution_id)),
                          populate_existing=True)
        if row is None or (row.deleted_at is not None and not include_deleted):
            raise ExecutionNotFoundError(str(execution_id))
        return row

    def _read(self, session: Session, execution_id: str) -> TaskExecution:
        return to_execution(self._load(session, execution_id))

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def _create(self, candidate: TaskExecution, *, reuse_placement: bool) -> TaskExecution:
        payload = ExecutionCreate(
            id=uuid.UUID(candidate.id), historical_reference=False, status=ExecutionStatus.SCHEDULED, sessions=[],
            **{name: getattr(candidate, name) for name in _SNAPSHOT_FIELDS
               if name in ExecutionCreate.model_fields and name != "status"},
        )
        with self._account.operation(self._translate(candidate.id)) as session:
            record_id = payload.id
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                existing = None
                if reuse_placement:
                    existing = session.scalars(select(models.Execution).where(
                        models.Execution.user_id == self._account.user_id,
                        models.Execution.scheduled_task_id == candidate.scheduled_task_id,
                    )).first()
                if existing is not None:
                    if existing.deleted_at is not None:
                        raise ExecutionDeletedError(str(existing.id))
                    record_id = existing.id  # never overwrites the original planned snapshot
                else:
                    mutator.create_execution(payload)
            return self._read(session, str(record_id))

    def create_canonical_execution(self, task: CanonicalTask, scheduled_task: CanonicalScheduledTask | None = None, *,
                                   user_id: uuid.UUID | None = None) -> TaskExecution:
        candidate = build_canonical_execution(task, scheduled_task, now=self._account.clock(), user_id=self._owner(user_id))
        return self._create(candidate, reuse_placement=False)

    def create_fixed_block_execution(self, block) -> TaskExecution:
        """The one execution of a fixed block (app/execution/fixed_block_completion.py); its id is derived."""
        from app.execution.fixed_block_completion import build_fixed_block_execution

        candidate = build_fixed_block_execution(block, now=self._account.clock(), user_id=self._owner(block.user_id))
        return self._create(candidate, reuse_placement=False)

    def get_or_create_canonical_execution(self, task: CanonicalTask, scheduled_task: CanonicalScheduledTask, *,
                                          user_id: uuid.UUID | None = None) -> TaskExecution:
        candidate = build_canonical_execution(task, scheduled_task, now=self._account.clock(), user_id=self._owner(user_id))
        return self._create(candidate, reuse_placement=True)

    def create_execution(self, *, task_name: str, category: str, tag: str, planned_date: int, planned_start: int,
                         planned_end: int, planned_duration: int, priority: int) -> TaskExecution:
        """A legacy (day-index snapshot) execution, as ExecutionService.create_execution."""
        now = self._account.clock().isoformat()
        candidate = TaskExecution(
            id=str(uuid.uuid4()), task_name=task_name, category=category, tag=tag, planned_date=planned_date,
            planned_start=planned_start, planned_end=planned_end, planned_duration=planned_duration, priority=priority,
            status=ExecutionStatus.SCHEDULED, created_at=now, updated_at=now, user_id=self._account.user_id,
        )
        return self._create(candidate, reuse_placement=False)

    def create_execution_from_scheduled_task(self, scheduled_task: ScheduledTask, *, planned_date: int,
                                             priority: int) -> TaskExecution:
        window = scheduled_task.time_window
        return self.create_execution(
            task_name=scheduled_task.name, category=scheduled_task.category, tag=scheduled_task.tag,
            planned_date=planned_date, planned_start=window.start_time, planned_end=window.end_time,
            planned_duration=window.end_time - window.start_time, priority=priority,
        )

    def get_or_create_execution(self, *, task_name: str, category: str, tag: str, planned_date: int,
                                planned_start: int, planned_end: int, planned_duration: int,
                                priority: int) -> TaskExecution:
        """As ExecutionService.get_or_create_execution: reuse the execution with this exact legacy snapshot."""
        snapshot = (task_name, category, tag, planned_date, planned_start, planned_end, planned_duration)
        for execution in self.list_executions():
            if (execution.task_name, execution.category, execution.tag, execution.planned_date,
                    execution.planned_start, execution.planned_end, execution.planned_duration) == snapshot:
                return execution
        return self.create_execution(task_name=task_name, category=category, tag=tag, planned_date=planned_date,
                                     planned_start=planned_start, planned_end=planned_end,
                                     planned_duration=planned_duration, priority=priority)

    # ------------------------------------------------------------------
    # Lifecycle (the server's mutation path; the shared transition table)
    # ------------------------------------------------------------------

    def _action(self, execution_id: str, action: str, expected_version: int | None) -> TaskExecution:
        with self._account.operation(self._translate(execution_id, expected_version, action)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                # Without a precondition the transition table is still checked against the stored status,
                # under the user's lock (as ExecutionService does).
                version = expected_version if expected_version is not None else self._load(session, execution_id).version
                mutator.execution_action(_execution_uuid(execution_id), action, ActionIn(base_version=version))
            return self._read(session, execution_id)

    def start(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "start", expected_version)

    def pause(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "pause", expected_version)

    def resume(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "resume", expected_version)

    def complete(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "complete", expected_version)

    def skip(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "skip", expected_version)

    def cancel(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "cancel", expected_version)

    def reopen(self, execution_id: str, *, expected_version: int | None = None) -> TaskExecution:
        return self._action(execution_id, "reopen", expected_version)

    def set_outcome(self, task: CanonicalTask, placement: CanonicalScheduledTask, outcome: TaskOutcome | str, *,
                    expected_version: int | None = None, require_version: bool = False) -> TaskExecution | None:
        """As ExecutionService.set_outcome, through the server's one implementation (backend/outcomes.py)."""
        fields = {"outcome": TaskOutcome(outcome)}
        if require_version:
            fields["base_version"] = expected_version  # present, even None: the precondition
        request = OutcomeIn(**fields)
        with self._account.operation(self._translate(str(placement.id), expected_version)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                set_placement_outcome(mutator, placement.id, request)
            row = session.scalars(select(models.Execution).where(
                models.Execution.user_id == self._account.user_id,
                models.Execution.scheduled_task_id == placement.id,
                models.Execution.deleted_at.is_(None),
            ).execution_options(populate_existing=True)).first()
            return to_execution(row) if row is not None else None

    def record_feedback(self, execution_id: str, *, expected_version: int, focus_rating: int | None = None,
                        energy_rating: int | None = None, interruption_count: int | None = None,
                        note: str | None = None) -> TaskExecution:
        check_feedback(focus_rating=focus_rating, energy_rating=energy_rating, interruption_count=interruption_count)
        values = {name: value for name, value in (("focus_rating", focus_rating), ("energy_rating", energy_rating),
                                                  ("interruption_count", interruption_count), ("note", note))
                  if value is not None}
        with self._account.operation(self._translate(execution_id, expected_version)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                mutator.execution_feedback(_execution_uuid(execution_id),
                                           FeedbackIn(base_version=expected_version, **values))
            return self._read(session, execution_id)

    def delete_execution(self, execution_id: str, *, expected_version: int) -> None:
        with self._account.operation(self._translate(execution_id, expected_version)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                mutator.delete_execution(_execution_uuid(execution_id), expected_version)

    def reset_all_history(self) -> int:
        """Tombstone every live execution of the account in one transaction; returns how many."""
        with self._account.operation(self._translate(None)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                rows = _live_rows(session, self._account.user_id, None)
                for row in rows:
                    mutator.delete_execution(row.id, row.version)
            return len(rows)

    def wire_id(self, execution_id: str) -> uuid.UUID:
        return _execution_uuid(execution_id)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_execution(self, execution_id: str) -> TaskExecution:
        with self._account.operation(self._translate(execution_id)) as session:
            return self._read(session, execution_id)

    def find_execution_for_placement(self, scheduled_task_id: uuid.UUID) -> TaskExecution | None:
        with self._account.operation(self._translate(None)) as session:
            row = session.scalars(select(models.Execution).where(
                models.Execution.user_id == self._account.user_id,
                models.Execution.scheduled_task_id == uuid.UUID(str(scheduled_task_id)),
                models.Execution.deleted_at.is_(None),
            )).first()
            return to_execution(row) if row is not None else None

    def set_outcomes(self, items, outcome: TaskOutcome | str) -> BulkOutcomeResult:
        """As ExecutionService.set_outcomes: one transaction through the server's own batch (backend/outcomes.py)."""
        with self._account.operation(self._translate(None)) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                return set_placements_outcome(mutator, [placement.id for _, placement in items], TaskOutcome(outcome))

    def executions_for_placements(self, placement_ids) -> dict[uuid.UUID, TaskExecution]:
        """The live executions of these placements, by placement id, in one query."""
        ids = [uuid.UUID(str(value)) for value in placement_ids]
        if not ids:
            return {}
        with self._account.operation(self._translate(None)) as session:
            rows = session.scalars(select(models.Execution).where(
                models.Execution.user_id == self._account.user_id,
                models.Execution.scheduled_task_id.in_(ids),
                models.Execution.deleted_at.is_(None),
            ))
            return {row.scheduled_task_id: to_execution(row) for row in rows}

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        with self._account.operation(self._translate(None)) as session:
            return [to_execution(row) for row in _live_rows(session, self._account.user_id, status)]

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        with self._account.operation(self._translate(execution_id)) as session:
            row = self._load(session, execution_id, include_deleted=True)
            return _sessions(session, self._account.user_id, [row.id])[row.id]


class DirectExecutionReader:
    """The two repository reads ProductivityService makes, over one consistent snapshot per list_executions()."""

    def __init__(self, account) -> None:
        self._account = account
        self._local = threading.local()

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        with self._account.operation() as session:
            rows = _live_rows(session, self._account.user_id, status)
            sessions = _sessions(session, self._account.user_id, [row.id for row in rows])
            self._local.sessions = {row.id: sessions[row.id] for row in rows}
            return [to_execution(row) for row in rows]

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        snapshot = getattr(self._local, "sessions", None)
        key = _execution_uuid(execution_id)
        if snapshot is not None and key in snapshot:
            return list(snapshot[key])
        with self._account.operation() as session:
            return _sessions(session, self._account.user_id, [key])[key]
