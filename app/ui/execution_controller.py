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
from collections.abc import Callable
from datetime import datetime, timezone

from app.execution.errors import ExecutionError, ExecutionVersionConflictError, InvalidTransitionError
from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus, TaskExecution
from app.execution import direct_completion
from app.execution.service import ExecutionService, compute_active_duration_minutes
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.ui.background import ControllerResult
from app.ui.execution_workflow import ExecutionItemView, describe_item

#: Lifecycle actions ExecutionController.perform runs (the Tk-free API for timed work; the Day page's board
#: moves tasks with set_outcome instead).
LIFECYCLE_ACTIONS = ("start", "pause", "resume", "complete", "skip", "cancel", "reopen")

# Which of start/pause/resume/complete/skip are valid from each status. This
# is a UI-facing view of app.execution.service's own transition rules (see
# ExecutionService._TRANSITIONS) -- expressed as action labels for enabling/
# disabling buttons, not as the transition mechanics themselves, which stay
# inside ExecutionService. Keep this in sync if the service's transition
# table ever changes.
_AVAILABLE_ACTIONS: dict[ExecutionStatus, tuple[str, ...]] = {
    ExecutionStatus.SCHEDULED: ("start", "complete", "skip"),
    ExecutionStatus.IN_PROGRESS: ("pause", "complete", "skip"),
    ExecutionStatus.PAUSED: ("resume", "complete", "skip"),
    ExecutionStatus.COMPLETED: ("reopen",),
    ExecutionStatus.SKIPPED: ("reopen",),
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
    def __init__(
        self,
        execution_service: ExecutionService,
        *,
        sync_state: Callable[[str], str | None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._service = execution_service
        #: execution id -> "local_only" / "pending" / "conflict" / "synced" / "server" (None: unknown).
        self._sync_state = sync_state
        self._clock = clock

    def describe(self, placement: CanonicalScheduledTask, execution: TaskExecution | None
                 ) -> ControllerResult[ExecutionItemView]:
        """A saved placement's execution state in words (reads only; never creates an execution)."""

        def op() -> ExecutionItemView:
            now = self._clock()
            active = None
            state = None
            if execution is not None:
                active = self._active_minutes(execution.id, now)
                state = self._sync_state(execution.id) if self._sync_state is not None else None
            return describe_item(placement, execution, now=now, active_minutes=active, sync_state=state)

        return self._call(op)

    def perform(
        self,
        task: CanonicalTask,
        placement: CanonicalScheduledTask,
        action: str,
        known: TaskExecution | None,
        *,
        feedback: dict | None = None,
    ) -> ControllerResult[TaskExecution]:
        """
        Run one lifecycle action the user chose on a saved placement. The
        first action creates its execution (get-or-create: never a duplicate);
        the version on screen is the precondition, so a change made elsewhere
        (another device, a sync, a second click) is reported, never
        overwritten. Optional feedback is recorded after complete/skip.
        """
        if action not in LIFECYCLE_ACTIONS:
            return ControllerResult.failure(f"Unknown action {action!r}.")

        def op() -> TaskExecution:
            if known is None:
                current = self._service.get_or_create_canonical_execution(task, placement)
            else:
                current = known
            result = getattr(self._service, action)(current.id, expected_version=current.version)
            values = {name: value for name, value in (feedback or {}).items() if value is not None}
            if values:
                result = self._service.record_feedback(current.id, expected_version=result.version, **values)
            return result

        return self._call(op)

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

    def set_outcomes(self, items, outcome: TaskOutcome | str) -> ControllerResult:
        """Every (task, placement) of `items` to the column `outcome`, in one transaction (a BulkOutcomeResult)."""
        return self._call(lambda: self._service.set_outcomes(list(items), outcome))

    def executions_for_placements(self, placement_ids) -> ControllerResult[dict[uuid.UUID, TaskExecution]]:
        """The live executions of these placements, by placement id (one read; never creates any)."""
        return self._call(lambda: self._service.executions_for_placements(list(placement_ids)))

    def _active_minutes(self, execution_id: str, now: datetime) -> float:
        sessions = self._service.list_sessions(execution_id)
        elapsed = compute_active_duration_minutes(sessions)
        open_session = next((session for session in sessions if session.ended_at is None), None)
        if open_session is not None:
            elapsed += (now - datetime.fromisoformat(open_session.started_at)).total_seconds() / 60
        return round(elapsed, 1)

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
