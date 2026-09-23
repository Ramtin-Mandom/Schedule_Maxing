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
from datetime import datetime, timezone

from app.execution.errors import ExecutionError
from app.execution.models import ExecutionStatus, TaskExecution
from app.execution.service import ExecutionService, compute_active_duration_minutes
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.ui.background import ControllerResult

# Which of start/pause/resume/complete/skip are valid from each status. This
# is a UI-facing view of app.execution.service's own transition rules (see
# ExecutionService._TRANSITIONS) -- expressed as action labels for enabling/
# disabling buttons, not as the transition mechanics themselves, which stay
# inside ExecutionService. Keep this in sync if the service's transition
# table ever changes.
_AVAILABLE_ACTIONS: dict[ExecutionStatus, tuple[str, ...]] = {
    ExecutionStatus.SCHEDULED: ("start", "skip"),
    ExecutionStatus.IN_PROGRESS: ("pause", "complete", "skip"),
    ExecutionStatus.PAUSED: ("resume", "complete", "skip"),
    ExecutionStatus.COMPLETED: (),
    ExecutionStatus.SKIPPED: (),
    # cancelled is terminal, same as completed/skipped. Listed explicitly
    # (rather than relying on a .get(..., ()) default) so a cancelled
    # execution loaded by the still-legacy UI (e.g. one created through the
    # new canonical creation API elsewhere) renders with no enabled actions
    # instead of raising a KeyError -- see Task 2's "cancelled data cannot
    # crash the still-legacy UI" requirement. A Cancel action/button is not
    # wired into the UI itself until Task 6.
    ExecutionStatus.CANCELLED: (),
}


class ExecutionController:
    def __init__(self, execution_service: ExecutionService) -> None:
        self._service = execution_service

    def available_actions(self, status: ExecutionStatus) -> tuple[str, ...]:
        """Pure lookup (no I/O): which actions should be enabled for an execution in this status."""
        return _AVAILABLE_ACTIONS[status]

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
        priority: int,
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
                priority=priority,
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

    def elapsed_active_minutes(
        self, execution_id: str, now: datetime | None = None
    ) -> ControllerResult[float]:
        """
        Live elapsed active time in minutes: every closed session (via
        ExecutionService's own compute_active_duration_minutes, reused rather
        than reimplemented) plus the currently-open session's elapsed-so-far,
        if the execution is in_progress. Paused time is never included, since
        it falls outside every session by construction.
        """

        def compute() -> float:
            sessions = self._service.list_sessions(execution_id)
            elapsed = compute_active_duration_minutes(sessions)

            open_session = next((session for session in sessions if session.ended_at is None), None)
            if open_session is not None:
                reference_now = now or datetime.now(timezone.utc)
                started_at = datetime.fromisoformat(open_session.started_at)
                elapsed += (reference_now - started_at).total_seconds() / 60

            return round(elapsed, 2)

        return self._call(compute)

    def get_execution(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.get_execution(execution_id))

    def list_executions(self, status: ExecutionStatus | None = None) -> ControllerResult[list[TaskExecution]]:
        return self._call(lambda: self._service.list_executions(status))

    def reset_all_history(self) -> ControllerResult[int]:
        return self._call(self._service.reset_all_history)

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except ExecutionError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net so DB/unexpected errors never crash the UI
            return ControllerResult.failure(f"Unexpected error: {error}")
