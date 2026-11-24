"""
execution_controller.py

UI-facing wrapper around app.execution.service.ExecutionService. This is the
only place UI widgets should call into execution tracking -- app/ui code
calls ExecutionController, never ExecutionService or ExecutionRepository
directly, preserving the UI -> service -> repository dependency direction.

Every method that can fail (i.e. touches the database) returns a
ControllerResult (see app/ui/background.py) instead of raising, so a Tk
callback can always check `.ok`/`.error` rather than risk an uncaught
exception reaching Tk's event loop.
"""

from __future__ import annotations

import uuid

from app.execution.errors import ExecutionError, ExecutionVersionConflictError, InvalidTransitionError
from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus, TaskExecution
from app.execution import direct_completion, fixed_block_completion
from app.execution.service import ExecutionService
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.ui.background import ControllerResult


class ExecutionController:
    def __init__(self, execution_service: ExecutionService) -> None:
        self._service = execution_service

    def set_outcome(
        self,
        task: CanonicalTask,
        placement: CanonicalScheduledTask,
        outcome: TaskOutcome | str,
        *,
        expected_version: int | None,
    ) -> ControllerResult[TaskExecution | None]:
        """
        Move a saved placement to the Uncompleted / Tasks / Completed column
        (its execution's lifecycle actions, one transaction; see
        ExecutionService.set_outcome). expected_version is the execution
        version on screen (None: none was shown), so a change made elsewhere
        is reported and reloaded, never overwritten.
        """
        return self._call(lambda: self._service.set_outcome(task, placement, outcome, expected_version=expected_version,
                                                            require_version=True))

    def complete_directly(self, task: CanonicalTask) -> ControllerResult[TaskExecution]:
        """Complete a task that has no placement (app/execution/direct_completion.py); repeating it changes nothing."""
        return self._call(lambda: direct_completion.complete_directly(self._service, task))

    def reopen_directly(self, task_id: uuid.UUID) -> ControllerResult[TaskExecution | None]:
        """Undo a direct completion of the task (None: it had none)."""
        return self._call(lambda: direct_completion.reopen_directly(self._service, task_id))

    def set_fixed_block_outcome(self, block, outcome: TaskOutcome | str) -> ControllerResult[TaskExecution | None]:
        """Move a fixed block to the Uncompleted / Tasks / Completed column (its one execution; idempotent)."""
        return self._call(lambda: fixed_block_completion.set_fixed_block_outcome(self._service, block, outcome))

    def fixed_block_executions(self, block_ids) -> ControllerResult[dict[uuid.UUID, TaskExecution]]:
        """The executions of these fixed blocks, by block id (blocks without one are absent; never creates any)."""
        def read() -> dict[uuid.UUID, TaskExecution]:
            found = ((block_id, fixed_block_completion.fixed_block_execution(self._service, block_id))
                     for block_id in block_ids)
            return {block_id: execution for block_id, execution in found if execution is not None}

        return self._call(read)

    def direct_executions(self, task_ids) -> ControllerResult[dict[uuid.UUID, TaskExecution]]:
        """The task-only execution in use of each of these tasks (To Dos), by task id; absent when it has none."""
        return self._call(lambda: direct_completion.direct_executions(self._service, list(task_ids)))

    def set_outcomes(self, items, outcome: TaskOutcome | str) -> ControllerResult:
        """Every (task, placement) of `items` to the column `outcome`, in one transaction (a BulkOutcomeResult)."""
        return self._call(lambda: self._service.set_outcomes(list(items), outcome))

    def executions_for_placements(self, placement_ids) -> ControllerResult[dict[uuid.UUID, TaskExecution]]:
        """The live executions of these placements, by placement id (one read; never creates any)."""
        return self._call(lambda: self._service.executions_for_placements(list(placement_ids)))

    def get_or_create_execution(
        self,
        *,
        task_name: str,
        category: str,
        tag: str,
        planned_date: int,
        planned_start: int,
        planned_end: int,
        planned_duration: int,
    ) -> ControllerResult[TaskExecution]:
        return self._call(
            lambda: self._service.get_or_create_execution(
                task_name=task_name,
                category=category,
                tag=tag,
                planned_date=planned_date,
                planned_start=planned_start,
                planned_end=planned_end,
                planned_duration=planned_duration,
            )
        )

    # Transitions take the version the caller is showing as their
    # precondition (see ExecutionService's "Preconditions" notes).

    def start(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.start(execution_id, expected_version=expected_version))

    def pause(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.pause(execution_id, expected_version=expected_version))

    def resume(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.resume(execution_id, expected_version=expected_version))

    def complete(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.complete(execution_id, expected_version=expected_version))

    def skip(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.skip(execution_id, expected_version=expected_version))

    def reopen(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.reopen(execution_id, expected_version=expected_version))

    def cancel(self, execution_id: str, *, expected_version: int | None = None) -> ControllerResult[TaskExecution]:
        """Not yet wired to a UI control (see Task 6); exposed so callers/tests can exercise it."""
        return self._call(lambda: self._service.cancel(execution_id, expected_version=expected_version))

    def get_or_create_canonical_execution(
        self,
        task: CanonicalTask,
        scheduled_task: CanonicalScheduledTask,
        *,
        user_id: uuid.UUID | None = None,
    ) -> ControllerResult[TaskExecution]:
        """
        Canonical (Task 2) identity-aware get-or-create, for a flexible
        placement: selecting the same placement again always resolves to
        the same execution, keyed by task_id/scheduled_task_id rather than
        a task-name lookup -- duplicate task names remain distinguishable.
        Only flexible ScheduledTask placements are tracked; fixed blocks are
        never passed here.
        """
        return self._call(
            lambda: self._service.get_or_create_canonical_execution(task, scheduled_task, user_id=user_id)
        )

    def find_execution_for_placement(self, scheduled_task_id: uuid.UUID) -> ControllerResult[TaskExecution | None]:
        """Look up (never create) the execution of a saved placement -- used to restore status on selection."""
        return self._call(lambda: self._service.find_execution_for_placement(scheduled_task_id))

    def create_canonical_execution(
        self,
        task: CanonicalTask,
        scheduled_task: CanonicalScheduledTask | None = None,
        *,
        user_id: uuid.UUID | None = None,
    ) -> ControllerResult[TaskExecution]:
        """Canonical (Task 2) plain creation -- always a new row; see ExecutionService.create_canonical_execution."""
        return self._call(lambda: self._service.create_canonical_execution(task, scheduled_task, user_id=user_id))

    def record_feedback(
        self,
        execution_id: str,
        *,
        expected_version: int,
        focus_rating: int | None = None,
        energy_rating: int | None = None,
        interruption_count: int | None = None,
        note: str | None = None,
    ) -> ControllerResult[TaskExecution]:
        return self._call(
            lambda: self._service.record_feedback(
                execution_id,
                expected_version=expected_version,
                focus_rating=focus_rating,
                energy_rating=energy_rating,
                interruption_count=interruption_count,
                note=note,
            )
        )

    def get_execution(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.get_execution(execution_id))

    def list_executions(self, status: ExecutionStatus | None = None) -> ControllerResult[list[TaskExecution]]:
        return self._call(lambda: self._service.list_executions(status))

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except ExecutionVersionConflictError as error:
            return ControllerResult.failure(
                "This task was changed elsewhere (another device, a sync, or a second click) since it was shown. "
                "Its saved state has been reloaded; check it and try again.", error)
        except InvalidTransitionError as error:
            return ControllerResult.failure(
                f"That action is not possible any more: {error} Its saved state has been reloaded.", error)
        except ExecutionError as error:
            return ControllerResult.failure(str(error), error)
        except Exception as error:  # noqa: BLE001 - last-resort safety net so DB/unexpected errors never crash the UI
            return ControllerResult.failure(f"Unexpected error: {error}", error)
