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

historical_plan answers "what was this planned work when it was planned":
the placement's own planning snapshot (app.planning.models.ScheduledTask),
else what its execution recorded when it was created, else unknown -- each
fact with its source, and never the task's current, mutable values.

Completion activity (CompletionHistory) is the second read model: the live
completed executions whose recorded completion instant lies in a range,
whatever their planned date -- including work whose placement or task was
later removed -- with each one's placement, the placements that superseded
it (so one occurrence completed twice is recognised) and their tasks.
HistoryBounds says where the recorded history starts and ends, so an
all-time report can be read in bounded windows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.execution.models import TaskExecution, WorkSession
from app.planning.models import ScheduledTask, Task, TaskType

#: A defensive bound on how far back a chain of moves/regenerations is followed.
MAX_LINEAGE_DEPTH = 64

#: HistoricalPlan.sources values: where a historical fact was recorded.
SOURCE_PLACEMENT = "placement_snapshot"
SOURCE_EXECUTION = "execution_snapshot"
#: The type the task has now: a stable identity, but assigned (or migrated) after the placement was saved.
SOURCE_CURRENT_TASK = "current_task"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class HistoricalPlan:
    """
    The recorded facts of one placement's task at planning time. A None value
    is unknown (its source says so); zero points and no tags are known values.
    """

    name: str | None
    category: str | None
    tags: tuple[str, ...] | None
    points: int | None
    estimate_minutes: int | None
    type_id: uuid.UUID | None
    type_label: str | None
    #: {fact name: SOURCE_*} for every fact above.
    sources: Mapping[str, str]


def historical_plan(
    placement: ScheduledTask, execution: TaskExecution | None = None, task: Task | None = None
) -> HistoricalPlan:
    """
    The historical facts of `placement` (see the module docstring), falling
    back to the snapshot of its `execution`. `task` (its current task record)
    contributes only the type identity of a placement saved before types were
    snapshotted -- never a name, category, tag list, points or estimate.
    """
    sources: dict[str, str] = {}

    def fact(name: str, planned, executed=None):
        if planned is not None:
            sources[name] = SOURCE_PLACEMENT
            return planned
        if executed is not None:
            sources[name] = SOURCE_EXECUTION
            return executed
        sources[name] = SOURCE_UNKNOWN
        return None

    recorded = execution if execution is not None else None
    name = fact("name", placement.task_name, recorded.task_name if recorded else None)
    category = fact("category", placement.task_category, recorded.category if recorded else None)
    tags = fact("tags", tuple(placement.task_tags) if placement.task_tags is not None else None)
    points = fact("points", placement.task_points, recorded.points if recorded else None)
    estimate = fact("estimate_minutes", placement.task_estimate_minutes,
                    recorded.planned_duration if recorded else None)
    type_id = fact("type_id", placement.task_type_id)
    type_label = fact("type_label", placement.task_type_label)
    if type_id is None and task is not None and task.task_type_id is not None:
        type_id, sources["type_id"] = task.task_type_id, SOURCE_CURRENT_TASK
    return HistoricalPlan(name=name, category=category, tags=tags, points=points, estimate_minutes=estimate,
                          type_id=type_id, type_label=type_label, sources=sources)


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
    #: The type records those tasks and placement snapshots name (deleted ones included), by id.
    task_types: Mapping[uuid.UUID, TaskType] = field(default_factory=dict)
    #: True when a chain of moves/regenerations was longer than MAX_LINEAGE_DEPTH: older plans are missing.
    lineage_truncated: bool = False


@dataclass(frozen=True)
class HistoryBounds:
    """The earliest and latest recorded instants of an owner's history (None: there is none)."""

    first_planned_start: datetime | None
    last_planned_start: datetime | None
    first_completion: datetime | None
    last_completion: datetime | None


@dataclass(frozen=True)
class CompletionHistory:
    start_utc: datetime
    end_utc: datetime
    #: Live completed executions whose recorded final end lies in [start_utc, end_utc), by (final end, id).
    executions: tuple[TaskExecution, ...]
    #: Their placements and every placement that superseded those (live and tombstoned), by id.
    placements: Mapping[uuid.UUID, ScheduledTask]
    tasks: Mapping[uuid.UUID, Task] = field(default_factory=dict)
    task_types: Mapping[uuid.UUID, TaskType] = field(default_factory=dict)
    #: Live completed executions of the owner with no recorded completion instant (any date): never dated.
    unknown_completion_dates: int = 0
    lineage_truncated: bool = False


def collect_schedule_history(
    start_utc: datetime,
    end_utc: datetime,
    *,
    in_range: Callable[[datetime, datetime], list[ScheduledTask]],
    superseded_by: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, list[ScheduledTask]]],
    executions_for: Callable[[Iterable[uuid.UUID]], list[ExecutionHistory]],
    tasks_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, Task]] | None = None,
    types_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, TaskType]] | None = None,
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
                           tasks=tasks, task_types=_types(placements.values(), tasks, types_for),
                           lineage_truncated=bool(frontier))


def _types(placements, tasks: Mapping[uuid.UUID, Task], types_for) -> dict[uuid.UUID, TaskType]:
    if types_for is None:
        return {}
    wanted = {p.task_type_id for p in placements if p.task_type_id is not None}
    wanted |= {task.task_type_id for task in tasks.values() if task.task_type_id is not None}
    return dict(types_for(wanted)) if wanted else {}


def collect_completion_history(
    start_utc: datetime,
    end_utc: datetime,
    *,
    completed_between: Callable[[datetime, datetime], list[TaskExecution]],
    placements_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, ScheduledTask]],
    tasks_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, Task]],
    types_for: Callable[[Iterable[uuid.UUID]], Mapping[uuid.UUID, TaskType]],
    unknown_completion_dates: int = 0,
) -> CompletionHistory:
    """Build a CompletionHistory from a repository's primitives (the caller holds one read snapshot)."""
    if end_utc <= start_utc:
        raise ValueError("end_utc must be after start_utc")
    executions = sorted(
        (item for item in completed_between(start_utc, end_utc)
         if item.actual_final_end_at is not None and start_utc <= item.actual_final_end_at < end_utc),
        key=lambda item: (item.actual_final_end_at, item.id),
    )
    placements: dict[uuid.UUID, ScheduledTask] = {}
    frontier = {item.scheduled_task_id for item in executions if item.scheduled_task_id is not None}
    for _ in range(MAX_LINEAGE_DEPTH):
        frontier -= set(placements)
        if not frontier:
            break
        found = placements_for(frontier)
        placements.update(found)
        frontier = {p.superseded_by_id for p in found.values() if p.superseded_by_id is not None}
    task_ids = {p.task_id for p in placements.values()} | {item.task_id for item in executions if item.task_id}
    tasks = dict(tasks_for(task_ids)) if task_ids else {}
    return CompletionHistory(
        start_utc=start_utc, end_utc=end_utc, executions=tuple(executions), placements=placements, tasks=tasks,
        task_types=_types(placements.values(), tasks, types_for), unknown_completion_dates=unknown_completion_dates,
        lineage_truncated=bool(frontier - set(placements)),
    )
