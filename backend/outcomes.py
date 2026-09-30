"""
backend/outcomes.py

The user's outcome for one saved placement -- the Day page's Uncompleted |
Tasks | Completed columns -- on the server. There is no second status
store: the outcome is the placement's TaskExecution status
(app/execution/lifecycle.py: outcome_of / outcome_actions), and a change is
the execution's own lifecycle actions (complete, skip, reopen), each an
ordinary versioned, change-logged mutation of the execution aggregate. So
a completion that is later reopened stays in the change log's revisions.

set_placement_outcome runs inside one Mutator (the user's lock and one
transaction): it resolves the placement among the authenticated user's own
live placements (another user's id is simply not found), creates the
placement's execution when it has none (never a duplicate), and applies
the actions. REST (POST /placements/{id}/outcome) and the direct desktop
storage both call it, so the rules are written once.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.execution.lifecycle import BulkOutcomeResult, OutcomeChangeError, TaskOutcome, outcome_actions, outcome_of
from app.execution.models import ExecutionStatus
from app.execution.service import build_canonical_execution
from backend import models
from backend.errors import ApiError
from backend.executions import EXECUTIONS, SNAPSHOT_FIELDS, ActionIn, ExecutionCreate, ExecutionOut
from backend.mutations import Mutator
from backend.planning_repository import ServerPlanningRepository
from backend.resources import Strict


class OutcomeIn(Strict):
    outcome: TaskOutcome
    #: The precondition when present: the execution version the client showed, or null when it showed
    #: none. Omitted: no precondition (the outcome is applied to whatever is stored).
    base_version: int | None = Field(default=None, gt=0)

    @property
    def checks_version(self) -> bool:
        return "base_version" in self.model_fields_set


class OutcomeOut(BaseModel):
    placement_id: uuid.UUID
    outcome: TaskOutcome
    #: The placement's execution after the change (None: still none, i.e. pending).
    execution: ExecutionOut | None = None


def placement_execution(mutator: Mutator, placement_id: uuid.UUID) -> models.Execution | None:
    """The live execution of one of the user's placements, if any."""
    return mutator.session.scalars(select(models.Execution).where(
        models.Execution.user_id == mutator.user_id,
        models.Execution.scheduled_task_id == placement_id,
        models.Execution.deleted_at.is_(None),
    )).first()


def set_placement_outcome(mutator: Mutator, placement_id: uuid.UUID, request: OutcomeIn) -> dict:
    repository = ServerPlanningRepository(mutator.session, mutator.user_id, lambda: mutator.now, mutator=mutator)
    placement = repository.get_placements([placement_id]).get(placement_id)
    if placement is None:
        raise ApiError(404, "not_found", "No live placement with this id.")
    task = repository.get_task(placement.task_id)
    if task is None:
        raise ApiError(422, "invalid_reference", "The placement's task no longer exists.")

    row = placement_execution(mutator, placement_id)
    current = EXECUTIONS.serialize(mutator.session, mutator.user_id, row) if row is not None else None
    if request.checks_version and (row.version if row is not None else None) != request.base_version:
        raise ApiError(409, "version_conflict", "The task's status was changed elsewhere since it was shown.",
                       supplied_version=request.base_version, current_version=row.version if row else None,
                       current=current)
    try:
        actions = outcome_actions(ExecutionStatus(row.status) if row is not None else None, request.outcome)
    except OutcomeChangeError as error:
        raise ApiError(409, "invalid_transition", str(error), current=current) from None

    if actions and row is None:
        candidate = build_canonical_execution(task, placement, now=mutator.now, user_id=mutator.user_id)
        mutator.create_execution(ExecutionCreate(
            id=uuid.UUID(candidate.id), historical_reference=False, status=ExecutionStatus.SCHEDULED, sessions=[],
            **{name: getattr(candidate, name) for name in SNAPSHOT_FIELDS
               if name in ExecutionCreate.model_fields and name != "status"},
        ))
        row = placement_execution(mutator, placement_id)
    for action in actions:
        mutator.execution_action(row.id, action, ActionIn(base_version=row.version))
        mutator.session.refresh(row)

    execution = EXECUTIONS.serialize(mutator.session, mutator.user_id, row) if row is not None else None
    outcome = outcome_of(ExecutionStatus(row.status) if row is not None else None)
    return OutcomeOut(placement_id=placement_id, outcome=outcome, execution=execution).model_dump(mode="json")


def set_placements_outcome(mutator: Mutator, placement_ids, outcome: TaskOutcome) -> BulkOutcomeResult:
    """
    Every listed placement of the user to `outcome` inside the caller's one
    mutation (one transaction): the Week/Month bulk actions. A cancelled
    attempt is left as it is (`skipped`); another user's id is not found and
    aborts the whole batch.
    """
    changed, unchanged, skipped = [], [], []
    for placement_id in placement_ids:
        row = placement_execution(mutator, placement_id)
        try:
            actions = outcome_actions(ExecutionStatus(row.status) if row is not None else None, outcome)
        except OutcomeChangeError:
            skipped.append(placement_id)
            continue
        if not actions:
            unchanged.append(placement_id)
            continue
        set_placement_outcome(mutator, placement_id, OutcomeIn(outcome=outcome))
        changed.append(placement_id)
    return BulkOutcomeResult(changed=tuple(changed), unchanged=tuple(unchanged), skipped=tuple(skipped))
