"""
app/execution/fixed_block_completion.py

Completing a fixed block. A fixed block is scheduled work at a time the user
set, and whether it was actually done is recorded like any other work: by an
execution, moved with the ordinary lifecycle actions (complete, skip,
reopen), counted by the same analytics with the block's points as they were
when the execution was created.

A block has exactly one execution, whose id is derived from the block's
(app.planning.models.fixed_block_execution_id). It names no task and no
placement -- a fixed block is neither -- and its planned times are the
block's own real ones. Nothing is created until the block is first moved out
of "pending".

Works with any execution service that has get_execution,
create_fixed_block_execution, complete, skip and reopen (the local and the
direct PostgreSQL service alike).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.execution.errors import ExecutionNotFoundError
from app.execution.lifecycle import TaskOutcome, outcome_actions
from app.execution.models import ExecutionStatus, TaskExecution
from app.planning.models import DEFAULT_TASK_PRIORITY, FixedBlock, fixed_block_execution_id


def build_fixed_block_execution(block: FixedBlock, *, now: datetime, user_id: uuid.UUID | None) -> TaskExecution:
    """A new 'scheduled' execution of `block`, with its snapshot (label, category, points, planned times)."""
    now_iso = now.isoformat()
    return TaskExecution(
        id=str(fixed_block_execution_id(block.id)),
        task_name=block.label,
        category=block.category,
        tag="",
        planned_duration=round((block.planned_end - block.planned_start).total_seconds() / 60),
        priority=DEFAULT_TASK_PRIORITY,
        points=block.points,
        status=ExecutionStatus.SCHEDULED,
        created_at=now_iso,
        updated_at=now_iso,
        user_id=user_id,
        canonical_planned_date=block.planned_date,
        canonical_timezone=block.timezone,
        canonical_planned_start=block.planned_start,
        canonical_planned_end=block.planned_end,
    )


def fixed_block_execution(service, block_id: uuid.UUID) -> TaskExecution | None:
    """The block's execution (None: it has none yet -- pending)."""
    try:
        return service.get_execution(str(fixed_block_execution_id(block_id)))
    except ExecutionNotFoundError:
        return None


def set_fixed_block_outcome(service, block: FixedBlock, outcome: TaskOutcome | str) -> TaskExecution | None:
    """
    Put `block` in the Uncompleted / Tasks / Completed column `outcome`: its
    execution is created when it has none (never duplicated) and driven there
    with lifecycle.outcome_actions. Idempotent. Returns the execution (None
    when a block without one stays pending).
    """
    existing = fixed_block_execution(service, block.id)
    actions = outcome_actions(existing.status if existing is not None else None, TaskOutcome(outcome))
    if not actions:
        return existing
    execution = existing or service.create_fixed_block_execution(block)
    for action in actions:
        execution = getattr(service, action)(execution.id, expected_version=execution.version)
    return execution
