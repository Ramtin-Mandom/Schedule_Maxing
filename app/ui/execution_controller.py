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

from datetime import datetime, timezone

from app.execution.errors import ExecutionError
from app.execution.models import ExecutionStatus, TaskExecution
from app.execution.service import ExecutionService, compute_active_duration_minutes
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

    def start(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.start(execution_id))

    def pause(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.pause(execution_id))

    def resume(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.resume(execution_id))

    def complete(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.complete(execution_id))

    def skip(self, execution_id: str) -> ControllerResult[TaskExecution]:
        return self._call(lambda: self._service.skip(execution_id))

    def record_feedback(
        self,
        execution_id: str,
        *,
        focus_rating: int | None = None,
        energy_rating: int | None = None,
        interruption_count: int | None = None,
        note: str | None = None,
    ) -> ControllerResult[TaskExecution]:
        return self._call(
            lambda: self._service.record_feedback(
                execution_id,
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
