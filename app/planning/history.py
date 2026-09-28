"""
app/planning/history.py

The read model analytics use for the planned-versus-actual history of a
date range (Milestone 5, docs/analytics.md): one bounded, owner-scoped,
consistent snapshot of

    - every placement -- live or tombstoned -- whose planned start lies in
      [start_utc, end_utc), and every placement those superseded, on any
      date (walking superseded_by_id back, docs/execution-rescheduling.md);
    - the live executions of the in-range placements, with their work
      sessions;
    - the tasks all those placements belong to (deleted tasks included), for
      display: names are never used to group or identify anything.

Each planning repository (SQLite: app/planning/repository.py; server and
direct PostgreSQL: backend/planning_repository.py) builds it with
collect_schedule_history inside one read snapshot, from three primitives,
so both storage paths answer identically. Reading never creates, changes or
deletes anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.execution.models import TaskExecution, WorkSession
from app.planning.models import ScheduledTask, Task

#: A defensive bound on how far back a chain of moves/regenerations is followed.
MAX_LINEAGE_DEPTH = 64


@dataclass(frozen=True)
class ExecutionHistory:
    execution: TaskExecution
    #: In chronological order.
    sessions: tuple[WorkSession, ...]


@dataclass(frozen=True)
class ScheduleHistory:
    start_utc: datetime
    end_utc: datetime
    #: In-range placements and all their predecessors (live and tombstoned), by id.
    placements: Mapping[uuid.UUID, ScheduledTask]
    #: Live executions of the in-range placements, by placement id (one per placement).
    executions: Mapping[uuid.UUID, ExecutionHistory]
    #: The placements' tasks by id, deleted ones included (display only).
    tasks: Mapping[uuid.UUID, Task] = field(default_factory=dict)


def collect_schedule_history(
    start_utc: datetime,
    end_utc: datetime,
    *,
    in_range: Callable[[datetime, datetime], list[ScheduledTask]],
    superseded_by: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, list[ScheduledTask]]],
    executions_for: Callable[[Iterable[uuid.UUID]], list[ExecutionHistory]],
    tasks_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, Task]] | None = None,
) -> ScheduleHistory:
    """Build a ScheduleHistory from a repository's primitives (the caller holds one read snapshot)."""
    if end_utc <= start_utc:
        raise ValueError("end_utc must be after start_utc")
    in_range_placements = in_range(start_utc, end_utc)
    placements = {placement.id: placement for placement in in_range_placements}
    frontier = list(placements)
    for _ in range(MAX_LINEAGE_DEPTH):
        if not frontier:
            break
        found = superseded_by(frontier)
        frontier = []
        for group in found.values():
            for placement in group:
                if placement.id not in placements:
                    placements[placement.id] = placement
                    frontier.append(placement.id)
    executions = {
        item.execution.scheduled_task_id: item
        for item in executions_for([placement.id for placement in in_range_placements])
        if item.execution.scheduled_task_id is not None
    }
    tasks = dict(tasks_for({p.task_id for p in placements.values()})) if tasks_for is not None else {}
    return ScheduleHistory(start_utc=start_utc, end_utc=end_utc, placements=placements, executions=executions,
                           tasks=tasks)
