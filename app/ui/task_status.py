"""
app/ui/task_status.py

The Day page's Uncompleted | Tasks | Completed board, Tk-free
(tests/ui/test_task_status.py). The widget is app/ui/task_status_board.py.

What belongs on the board: the tasks the saved schedule actually placed on
the date -- its live placements (DaySnapshot.executables) -- and the date's
fixed blocks, which are scheduled work too (at a time the user set) and are
completed the same way (app/execution/fixed_block_completion.py). A task
the scheduler could not place was never scheduled; it stays with the Day
page's "Available tasks" and is never Uncompleted.

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
from app.planning.models import FixedBlock, ScheduledTask, Task
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
    #: A fixed block's card: the block (task and placement are then None; `execution` is its one execution).
    block: FixedBlock | None = None

    @property
    def key(self) -> uuid.UUID:
        if self.block is not None:
            return self.block.id
        return self.placement.id if self.placement is not None else uuid.UUID(self.execution.id)

    @property
    def direct(self) -> bool:
        """Completed without a time slot: from its project, or a To Do."""
        return self.placement is None and self.block is None

    @property
    def outcome(self) -> TaskOutcome:
        return outcome_of(self.execution.status if self.execution is not None else None)

    @property
    def name(self) -> str:
        if self.block is not None:
            return self.block.label
        return self.display_name or (self.task.name if self.task is not None else self.execution.task_name)

    @property
    def category(self) -> str:
        if self.block is not None:
            return self.block.category
        if self.placement is None:
            return self.execution.category
        return self.placement.task_category or self.task.category

    @property
    def points(self) -> int | None:
        """What completing it is worth: the attempt's snapshot once it has one, else the current value."""
        if self.execution is not None and self.execution.points is not None:
            return self.execution.points
        if self.block is not None:
            return self.block.points
        return self.task.points if self.task is not None else None

    @property
    def _is_todo(self) -> bool:
        return self.direct and self.task is not None and self.task.is_todo

    @property
    def time_text(self) -> str:
        if self.direct:
            return "To Do" if self._is_todo else "From its project"
        return f"{format_clock(self.start_minute)} – {format_clock(self.end_minute)}"

    @property
    def detail(self) -> str:
        if self.direct:
            what = "To Do" if self._is_todo else "Completed from its project (no time slot)"
            return f"{what} · {self.category}{self._points_text}"
        fixed = " · fixed" if self.block is not None else ""
        return (f"{self.time_text} · {format_duration(self.end_minute - self.start_minute)} · {self.category}"
                f"{fixed}{self._points_text}")

    @property
    def _points_text(self) -> str:
        return "" if self.points is None else f" · {self.points} pts"

    @property
    def sort_instant(self):
        if self.block is not None:
            return self.block.planned_start
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


@dataclass(frozen=True)
class DayPoints:
    """A date's points: earned by what is completed, out of everything that belongs to the date."""

    completed: int = 0
    possible: int = 0
    completed_count: int = 0
    incomplete_count: int = 0

    @property
    def text(self) -> str:
        return f"{self.completed} / {self.possible} pts"


def day_points(board: StatusBoard, todos=(), todo_executions: dict[uuid.UUID, TaskExecution] | None = None,
               unplaced_points: int = 0, unplaced_count: int = 0) -> DayPoints:
    """
    The date's Completed / Possible points, from the same records the board
    and the productivity statistics read (executions and their points
    snapshots; nothing is stored for it): the board's scheduled tasks and
    fixed blocks, the date's To Dos (`todo_executions`: their completion
    records by task id), and the date's own tasks that are not scheduled
    (possible only). Each item counts once: a To Do completed today is also a
    card in Completed, and is counted as the To Do it is.
    """
    completed = possible = done = pending = 0
    items = [(card.points or 0, card.outcome == TaskOutcome.COMPLETED) for card in board.cards if not card.direct]
    for task in todos:
        execution = (todo_executions or {}).get(task.id)
        finished = execution is not None and execution.status == ExecutionStatus.COMPLETED
        items.append((execution.points if finished and execution.points is not None else task.points, finished))
    for points, finished in items:
        possible += points
        completed += points if finished else 0
        done += finished
        pending += not finished
    return DayPoints(completed=completed, possible=possible + unplaced_points, completed_count=done,
                     incomplete_count=pending + unplaced_count)


def build_board(day: date_ | None, executables, executions: dict[uuid.UUID, TaskExecution], direct=(),
                blocks=(), block_executions: dict[uuid.UUID, TaskExecution] | None = None) -> StatusBoard:
    """
    The board of a date from its saved placements (ExecutablePlacement) and
    their executions, its fixed blocks (`blocks`) and theirs
    (`block_executions`, by block id), plus the tasks completed that day
    without a time slot (`direct`: DirectCompletion records -- completed from
    a project, or To Dos), which appear in Completed.
    """
    cards = []
    for block in blocks:
        start = local_minutes(block.planned_start, block.planned_date, block.timezone)
        end = local_minutes(block.planned_end, block.planned_date, block.timezone) or MINUTES_PER_DAY
        cards.append(StatusCard(task=None, placement=None, execution=(block_executions or {}).get(block.id),
                                start_minute=start, end_minute=end, block=block))
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

    def board(self, day: date_ | None, executables, direct=(), blocks=(), todos=()) -> ControllerResult[StatusBoard]:
        """`todos`: the day's To Dos -- its completed ones are listed in Completed, on this (their own) day."""
        found = self._executions.executions_for_placements([item.placement.id for item in executables])
        if not found.ok:
            return ControllerResult.failure(found.error, found.cause)
        if todos:
            from app.ui.schedule_page_controller import todo_completions

            ticked = self.todo_executions(todos)
            if not ticked.ok:
                return ControllerResult.failure(ticked.error, ticked.cause)
            direct = [*direct, *todo_completions(todos, ticked.value)]
        of_blocks = self._executions.fixed_block_executions([block.id for block in blocks])
        if not of_blocks.ok:
            return ControllerResult.failure(of_blocks.error, of_blocks.cause)
        return ControllerResult.success(build_board(day, executables, found.value, direct, blocks, of_blocks.value))

    def todo_executions(self, tasks) -> ControllerResult[dict[uuid.UUID, TaskExecution]]:
        """The completion record in use of each of these To Dos, by task id (absent: never answered)."""
        return self._executions.direct_executions([task.id for task in tasks])

    def set_todo_done(self, task: Task, done: bool) -> ControllerResult[TaskExecution | None]:
        """Complete a To Do (awarding its points) or take the completion back; repeating either changes nothing."""
        if done:
            return self._executions.complete_directly(task)
        return self._executions.reopen_directly(task.id)

    def move(self, card: StatusCard, target: TaskOutcome) -> ControllerResult[TaskExecution | None]:
        """Put `card`'s placement in the `target` column (persisted; a conflict is reported, not overwritten)."""
        if card.block is not None:
            return self._executions.set_fixed_block_outcome(card.block, target)
        if card.direct:
            # Completed without a time slot (from its project, or a To Do): the only move is undoing it.
            if target != TaskOutcome.PENDING:
                return ControllerResult.failure("A task completed without a time slot can only go back to not "
                                                "completed.")
            return self._executions.reopen(card.execution.id, expected_version=card.execution.version)
        return self._executions.set_outcome(card.task, card.placement, target, expected_version=card.execution_version)
