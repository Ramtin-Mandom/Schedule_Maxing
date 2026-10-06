"""
app/ui/task_status.py

The Day page's Uncompleted | Tasks | Completed board, Tk-free
(tests/ui/test_task_status.py). The widget is app/ui/task_status_board.py.

What belongs on the board: only tasks the saved schedule actually placed on
the date -- its live placements (DaySnapshot.executables). A task the
scheduler could not place was never scheduled; it stays with the Day page's
"Available tasks" and is never Uncompleted.

Where the column comes from: nowhere but the placement's TaskExecution
(app/execution/lifecycle.py: outcome_of). No execution, or one that is
scheduled / in progress / paused, is Tasks; completed is Completed; skipped
(and a cancelled attempt) is Uncompleted. Everything is keyed by placement
id -- never by task name or list position -- so two tasks with the same
name, or two occurrences of a recurring task, never share a status.

Why a re-run keeps the columns: generation never writes executions and
keeps every placement whose attempt has history (completed, skipped,
started) exactly where it is, with its id; an incremental Make Schedule
keeps every placement that still fits. So Completed and Uncompleted stay
put, and only newly placed tasks appear -- in Tasks, since they have no
execution yet. A pending placement a full regeneration replaces is simply
the new placement, pending too.

Moves (TaskStatusController.move) are one ExecutionController.set_outcome
call each: the execution's lifecycle actions in one transaction, with the
execution version shown as the precondition.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_

from app.execution.lifecycle import TaskOutcome, outcome_of
from app.execution.models import ExecutionStatus, TaskExecution
from app.planning.models import ScheduledTask, Task
from app.planning.time import local_minutes
from app.ui.background import ControllerResult
from app.ui.time_fields import MINUTES_PER_DAY, format_clock, format_duration

#: The board's columns, left to right, and their headings.
COLUMNS: tuple[TaskOutcome, ...] = (TaskOutcome.UNCOMPLETED, TaskOutcome.PENDING, TaskOutcome.COMPLETED)
COLUMN_TITLES = {TaskOutcome.UNCOMPLETED: "Uncompleted", TaskOutcome.PENDING: "Tasks",
                 TaskOutcome.COMPLETED: "Completed"}


@dataclass(frozen=True)
class StatusCard:
    #: None only for a task completed from its project that was deleted since (the execution still names it).
    task: Task | None
    #: None: completed without a time slot (from its project) -- `execution` is then its completion.
    placement: ScheduledTask | None
    #: The placement's execution now (None: none yet -- pending).
    execution: TaskExecution | None
    start_minute: int
    end_minute: int
    #: The name as schedule views show it (with the project's abbreviation); "" = the task's own name.
    display_name: str = ""

    @property
    def key(self) -> uuid.UUID:
        return self.placement.id if self.placement is not None else uuid.UUID(self.execution.id)

    @property
    def direct(self) -> bool:
        """Completed without a time slot, from its project."""
        return self.placement is None

    @property
    def outcome(self) -> TaskOutcome:
        return outcome_of(self.execution.status if self.execution is not None else None)

    @property
    def name(self) -> str:
        return self.display_name or (self.task.name if self.task is not None else self.execution.task_name)

    @property
    def category(self) -> str:
        if self.placement is None:
            return self.execution.category
        return self.placement.task_category or self.task.category

    @property
    def time_text(self) -> str:
        if self.placement is None:
            return "From its project"
        return f"{format_clock(self.start_minute)} – {format_clock(self.end_minute)}"

    @property
    def detail(self) -> str:
        if self.placement is None:
            return f"Completed from its project (no time slot) · {self.category}"
        return f"{self.time_text} · {format_duration(self.end_minute - self.start_minute)} · {self.category}"

    @property
    def sort_instant(self):
        return self.placement.planned_start if self.placement is not None else self.execution.actual_final_end_at

    @property
    def execution_version(self) -> int | None:
        return self.execution.version if self.execution is not None else None

    @property
    def can_move(self) -> bool:
        """A cancelled attempt (withdrawn, not the user's answer) stays where it is."""
        return self.execution is None or self.execution.status != ExecutionStatus.CANCELLED

    @property
    def status_note(self) -> str:
        """Extra words for states the three columns alone do not say."""
        if self.execution is None:
            return ""
        return {ExecutionStatus.IN_PROGRESS: "in progress", ExecutionStatus.PAUSED: "paused",
                ExecutionStatus.CANCELLED: "cancelled"}.get(self.execution.status, "")


@dataclass(frozen=True)
class StatusBoard:
    day: date_ | None = None
    cards: list[StatusCard] = field(default_factory=list)

    def column(self, outcome: TaskOutcome) -> list[StatusCard]:
        return [card for card in self.cards if card.outcome == outcome]

    def card(self, placement_id: uuid.UUID) -> StatusCard | None:
        return next((card for card in self.cards if card.key == placement_id), None)

    @property
    def counts(self) -> dict[TaskOutcome, int]:
        return {outcome: len(self.column(outcome)) for outcome in COLUMNS}


def build_board(day: date_ | None, executables, executions: dict[uuid.UUID, TaskExecution], direct=()
                ) -> StatusBoard:
    """
    The board of a date from its saved placements (ExecutablePlacement) and
    their executions, plus the tasks completed that day without a time slot
    (`direct`: DirectCompletion records), which appear in Completed.
    """
    cards = []
    for item in executables:
        placement = item.placement
        start = local_minutes(placement.planned_start, placement.planned_date, placement.timezone)
        end = local_minutes(placement.planned_end, placement.planned_date, placement.timezone) or MINUTES_PER_DAY
        cards.append(StatusCard(task=item.task, placement=placement, execution=executions.get(placement.id),
                                start_minute=start, end_minute=end,
                                display_name=getattr(item, "display_name", "")))
    for item in direct:
        cards.append(StatusCard(task=item.task, placement=None, execution=item.execution, start_minute=0,
                                end_minute=0, display_name=item.display_name))
    cards.sort(key=lambda card: (card.sort_instant, card.name.lower(), str(card.key)))
    return StatusBoard(day=day, cards=cards)


class TaskStatusController:
    """Reads and changes the board through ExecutionController (the execution services; no own state)."""

    def __init__(self, execution_controller) -> None:
        self._executions = execution_controller

    def board(self, day: date_ | None, executables, direct=()) -> ControllerResult[StatusBoard]:
        found = self._executions.executions_for_placements([item.placement.id for item in executables])
        if not found.ok:
            return ControllerResult.failure(found.error, found.cause)
        return ControllerResult.success(build_board(day, executables, found.value, direct))

    def move(self, card: StatusCard, target: TaskOutcome) -> ControllerResult[TaskExecution | None]:
        """Put `card`'s placement in the `target` column (persisted; a conflict is reported, not overwritten)."""
        if card.direct:
            # Completed from its project without a time slot: the only move is undoing that completion.
            if target != TaskOutcome.PENDING:
                return ControllerResult.failure("A task completed from its project has no time slot here; it can "
                                                "only go back to not completed.")
            return self._executions.reopen(card.execution.id, expected_version=card.execution.version)
        return self._executions.set_outcome(card.task, card.placement, target, expected_version=card.execution_version)
