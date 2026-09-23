"""
app/planning/external_dependencies.py

Resolving dependencies that lie *outside* the range being scheduled
(Milestone 3). Range scheduling only loads the tasks in the range's own
scope, so a prerequisite planned for another week used to be reported as
"unresolved" even when it had long been completed. This module turns
persisted facts -- the dependency's task row, its active placements, and
its execution history -- into one explicit state per external dependency,
which the controller feeds into the existing interfaces:
app.planning.allocation.allocate_tasks(external_dependency_dates=...) and
app.planning.service.generate_selected_day(external_dependency_satisfaction=...)
(which becomes the day engine's external_dependency_ends). Scoring and
Greedy Optimizer v1 are untouched.

States, checked in this order (the first that applies wins):

    MISSING         no live persisted task has this id (never stored, or
                    deleted). Blocks its dependents.
    COMPLETED       some live execution of the task is `completed`.
                    Satisfied at the earliest completion instant
                    (actual_final_end_at).
    SCHEDULED       an active (live) placement of the task on a date *before*
                    the range, whose execution (if any) was not skipped or
                    cancelled. These are earlier *generated results* -- a
                    mere allocation assignment is never taken as proof that
                    a dependency will happen. Satisfied at that placement's
                    planned_end (the earliest one, if several).
    SCHEDULED_LATER an active placement exists only on a date *after* the
                    range. Blocks its dependents within this range.
    SKIPPED /       the task's most recent terminal execution was skipped /
    CANCELLED       cancelled (and nothing above applies). Blocks.
    PENDING         the task exists but is neither done nor usefully
                    scheduled (e.g. never scheduled, or only in progress
                    without a placement). Blocks.

Placements dated inside the range are deliberately ignored: range
scheduling replaces them, so they cannot vouch for anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from app.planning.allocation import AllocationDiagnostic, AllocationReasonCode, AllocationResult
from app.planning.models import ScheduledTask, Task


class ExternalDependencyState(str, Enum):
    MISSING = "missing"
    COMPLETED = "completed"
    SCHEDULED = "scheduled"
    SCHEDULED_LATER = "scheduled_later"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    PENDING = "pending"


_SATISFYING = {ExternalDependencyState.COMPLETED, ExternalDependencyState.SCHEDULED}

_EXPLANATIONS = {
    ExternalDependencyState.MISSING: "does not exist (it was never saved, or it was deleted)",
    ExternalDependencyState.SCHEDULED_LATER: "is only scheduled after this date range",
    ExternalDependencyState.SKIPPED: "was skipped, so it is not done",
    ExternalDependencyState.CANCELLED: "was cancelled, so it is not done",
    ExternalDependencyState.PENDING: "is outside this date range and is neither completed nor scheduled before it",
}


@dataclass(frozen=True)
class ExecutionFact:
    """The part of one execution row that dependency resolution reads."""

    task_id: uuid.UUID
    scheduled_task_id: uuid.UUID | None
    status: str
    finished_at: datetime | None
    updated_at: str


@dataclass(frozen=True)
class ExternalDependency:
    task_id: uuid.UUID
    state: ExternalDependencyState
    #: Local date (planning timezone) from which the dependency is satisfied; None if it blocks.
    satisfied_date: date_ | None = None
    #: Exact instant from which the dependency is satisfied; None if it blocks.
    satisfied_at: datetime | None = None

    @property
    def satisfied(self) -> bool:
        return self.state in _SATISFYING

    def explanation(self) -> str:
        if self.state == ExternalDependencyState.COMPLETED:
            return f"was completed at {self.satisfied_at.isoformat()}"
        if self.state == ExternalDependencyState.SCHEDULED:
            return f"is scheduled to finish at {self.satisfied_at.isoformat()}"
        return _EXPLANATIONS[self.state]

    def fingerprint_payload(self) -> list:
        return [
            self.state.value,
            self.satisfied_date.isoformat() if self.satisfied_date else None,
            self.satisfied_at.astimezone(timezone.utc).isoformat() if self.satisfied_at else None,
        ]


def external_dependency_ids(tasks: Iterable[Task]) -> set[uuid.UUID]:
    """Dependency ids referenced by `tasks` that are not themselves among `tasks`."""
    tasks = list(tasks)
    local = {task.id for task in tasks}
    return {dependency for task in tasks for dependency in task.dependency_ids if dependency not in local}


def resolve_external_dependencies(
    dependency_ids: Iterable[uuid.UUID],
    *,
    range_start: date_,
    range_end: date_,
    timezone_name: str,
    persisted_task_ids: set[uuid.UUID],
    placements_by_task: Mapping[uuid.UUID, list[ScheduledTask]],
    executions_by_task: Mapping[uuid.UUID, list[ExecutionFact]],
) -> dict[uuid.UUID, ExternalDependency]:
    """Classify each external dependency (see the module docstring). Pure: reads only its arguments."""
    zone = ZoneInfo(timezone_name)
    resolved: dict[uuid.UUID, ExternalDependency] = {}

    for dependency_id in sorted(set(dependency_ids), key=str):
        if dependency_id not in persisted_task_ids:
            resolved[dependency_id] = ExternalDependency(dependency_id, ExternalDependencyState.MISSING)
            continue

        executions = executions_by_task.get(dependency_id, [])
        completions = [fact.finished_at for fact in executions if fact.status == "completed" and fact.finished_at]
        if completions:
            finished = min(completions)
            resolved[dependency_id] = ExternalDependency(
                dependency_id, ExternalDependencyState.COMPLETED,
                satisfied_date=finished.astimezone(zone).date(), satisfied_at=finished,
            )
            continue

        abandoned = {
            fact.scheduled_task_id for fact in executions
            if fact.status in ("skipped", "cancelled") and fact.scheduled_task_id is not None
        }
        usable = [
            placement for placement in placements_by_task.get(dependency_id, [])
            if placement.id not in abandoned and not range_start <= placement.planned_date <= range_end
        ]
        before = [placement for placement in usable if placement.planned_date < range_start]
        if before:
            earliest = min(before, key=lambda placement: (placement.planned_end, str(placement.id)))
            resolved[dependency_id] = ExternalDependency(
                dependency_id, ExternalDependencyState.SCHEDULED,
                satisfied_date=earliest.planned_date, satisfied_at=earliest.planned_end,
            )
            continue
        if usable:
            resolved[dependency_id] = ExternalDependency(dependency_id, ExternalDependencyState.SCHEDULED_LATER)
            continue

        terminal = sorted(
            (fact for fact in executions if fact.status in ("skipped", "cancelled")), key=lambda fact: fact.updated_at
        )
        if terminal:
            state = ExternalDependencyState.SKIPPED if terminal[-1].status == "skipped" else ExternalDependencyState.CANCELLED
            resolved[dependency_id] = ExternalDependency(dependency_id, state)
            continue

        resolved[dependency_id] = ExternalDependency(dependency_id, ExternalDependencyState.PENDING)

    return resolved


def allocation_dates(external: Mapping[uuid.UUID, ExternalDependency]) -> dict[uuid.UUID, date_]:
    """allocate_tasks' external_dependency_dates: only satisfied dependencies (the rest must keep blocking)."""
    return {task_id: dependency.satisfied_date for task_id, dependency in external.items() if dependency.satisfied}


def satisfaction_instants(external: Mapping[uuid.UUID, ExternalDependency]) -> dict[uuid.UUID, tuple[date_, datetime]]:
    """generate_selected_day's external_dependency_satisfaction: (date, instant) for satisfied dependencies."""
    return {
        task_id: (dependency.satisfied_date, dependency.satisfied_at)
        for task_id, dependency in external.items()
        if dependency.satisfied
    }


def ceil_to_minute(instant: datetime) -> datetime:
    """The day engine works in whole minutes; a dependency is only safely done from the next whole minute."""
    floored = instant.replace(second=0, microsecond=0)
    return floored if floored == instant else floored + timedelta(minutes=1)


def explain_unallocated(
    allocation: AllocationResult,
    tasks: Mapping[uuid.UUID, Task],
    external: Mapping[uuid.UUID, ExternalDependency],
) -> AllocationResult:
    """
    Make blocked-by-external-dependency entries say *why* (completed vs.
    skipped vs. cancelled vs. pending vs. missing), and add one diagnostic
    per resolved external dependency. The blocking dependency is found
    exactly the way allocate_tasks finds it: the first dependency (in
    dependency_ids order) that is neither allocated nor satisfied
    externally. Allocation's decisions themselves are unchanged.
    """
    satisfied = allocation_dates(external)
    unallocated = []
    for entry in allocation.unallocated:
        task = tasks.get(entry.task_id)
        if entry.reason_code == AllocationReasonCode.DEPENDENCY_UNRESOLVED and task is not None:
            blocker = next(
                (
                    dependency for dependency in task.dependency_ids
                    if dependency not in allocation.assignments and dependency not in satisfied
                ),
                None,
            )
            if blocker is not None and blocker in external:
                entry = entry.model_copy(
                    update={"explanation": f"depends on {blocker}, which {external[blocker].explanation()}."}
                )
        unallocated.append(entry)

    diagnostics = list(allocation.diagnostics) + [
        AllocationDiagnostic(task_id=task_id, message=f"external dependency {task_id} {dependency.explanation()}")
        for task_id, dependency in sorted(external.items(), key=lambda item: str(item[0]))
    ]
    return allocation.model_copy(update={"unallocated": unallocated, "diagnostics": diagnostics})
