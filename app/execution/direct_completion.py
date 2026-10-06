"""
app/execution/direct_completion.py

Completing a task that has no saved placement -- the Projects panel's
exception to "a task is completed on its scheduled time slot". Nothing is
scheduled to allow it: the completion is a task-only execution (an execution
of the task with no placement, which ExecutionService has always supported),
completed and reopened with the ordinary lifecycle actions. It is therefore
counted by the same analytics as every other completion, on the date it was
completed, with the task's points as they were then.

Rules (the caller decides *which* tasks may use this; see
app/ui/projects_controller.py):

    - one task has at most one live task-only attempt in use: completing
      again reuses it (a completed one is returned as it is, a reopened one
      is completed again), so repeating the action never adds a completion
      or counts points twice;
    - undoing is the lifecycle's own reopen: the completion marker and its
      metrics are withdrawn, and with them its place in every statistic;
    - a cancelled attempt is never revived (nothing leaves cancelled).

Works with any execution service that has list_executions,
create_canonical_execution, complete and reopen (the local and the direct
PostgreSQL service alike).
"""

from __future__ import annotations

import uuid

from app.execution.models import ExecutionStatus, TaskExecution


def direct_execution(service, task_id: uuid.UUID) -> TaskExecution | None:
    """The task's live task-only execution in use: its completed one, else its newest one that can be completed."""
    own = [execution for execution in service.list_executions()
           if execution.task_id == task_id and execution.scheduled_task_id is None
           and execution.status != ExecutionStatus.CANCELLED]
    completed = [execution for execution in own if execution.status == ExecutionStatus.COMPLETED]
    if completed:
        return completed[-1]
    usable = [execution for execution in own if execution.status != ExecutionStatus.SKIPPED]
    return usable[-1] if usable else None


def complete_directly(service, task) -> TaskExecution:
    """Complete `task` without a placement (see the module docstring); idempotent."""
    execution = direct_execution(service, task.id)
    if execution is not None and execution.status == ExecutionStatus.COMPLETED:
        return execution
    if execution is None:
        execution = service.create_canonical_execution(task, None)
    return service.complete(execution.id, expected_version=execution.version)


def reopen_directly(service, task_id: uuid.UUID) -> TaskExecution | None:
    """Undo complete_directly; None when the task has no direct completion (nothing changes)."""
    execution = direct_execution(service, task_id)
    if execution is None or execution.status != ExecutionStatus.COMPLETED:
        return None
    return service.reopen(execution.id, expected_version=execution.version)
