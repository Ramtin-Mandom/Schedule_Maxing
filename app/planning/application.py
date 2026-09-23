"""
app/planning/application.py

The persistence-backed planning application service (Milestone 2, made
sync-ready in Milestone 3): the one boundary through which callers
(app/ui/planning_controller.py, the CLI, CSV import/export) read and write
persisted canonical planning data. It owns the business rules around
storage -- reference checks, deletion policy, version/audit bookkeeping,
optimistic concurrency, scoped placement replacement and rescheduling,
persisted preference layers and schedule provenance, and date-range
eligibility -- while app/planning/repository.py only maps rows. Scheduling
itself stays in app/planning/allocation.py, app/planning/service.py
(selected-day generation), and app/optimizer.py; nothing here schedules or
scores.

Snapshots: every returned model is freshly loaded from the database.
Mutating a returned Task/FixedBlock/ScheduledTask (or one passed in) never
changes stored state; only an explicit save does.

Optimistic concurrency (replaces Milestone 2's last-write-wins):
    - Every update and delete of a stored record states the version the
      caller last read: `expected_version=` (or `expected_versions=` for a
      batch). The write is an atomic compare-and-update in SQLite
      (repository update_*/soft_delete_*); if the stored record is at any
      other version, or was deleted meanwhile, VersionConflictError is raised
      and the stored record is left exactly as it was. The version carried
      *inside* a model passed in is never trusted as a precondition and can
      never push the stored version (the old `max(stored, given) + 1` rule
      let a stale or crafted version win; it is gone).
    - Creating (expected_version omitted) stores the record exactly as given
      (its own id, created_at, updated_at, version -- e.g. an
      identity-preserving import keeps them). An id that already exists,
      live or as a tombstone, is a DuplicateEntityError: a create can never
      overwrite anything.
    - Local revision rule: each *logical* mutation of a record that changes
      its content advances its `version` by exactly 1 (and sets updated_at
      to the service clock; created_at is never changed). Saving unchanged
      content still checks the precondition but is a no-op. Soft deletion
      is a logical mutation too (+1). `version` is this device's local edit
      revision, not a server version (see docs/sync-contract.md).
    - Ownership (`user_id`) is immutable through updates; placements always
      carry their task's owner.

Deletion is soft (tombstones, schema v4): a deleted record keeps its row
with `deleted_at` set, and disappears from every normal read. Tombstoned ids
can never be reused by a create.
    - Task: refused (EntityInUseError) while another *live* task depends on
      it -- deleting a dependency must never silently unblock its
      dependents. Deleting both in one delete_tasks call is fine. The task's
      active placements (any date) are tombstoned with it; its child rows
      stay with the tombstone.
    - Project: refused (EntityInUseError) while any live task references it.
    - Placement: replace_placements(start, end, placements) makes the live
      placements dated within [start, end] exactly `placements` (create/
      update by id, tombstone the rest *of that range only*). Placements
      dated outside the range are never touched; a placement whose id is
      already stored on a date outside the range is rejected (ScopeError)
      rather than moved. reschedule_range (Make Schedule) additionally
      requires the in-range placements the generation read as its
      precondition, removes superseded placements outside the range (see
      app/planning/occurrence.py), and saves the range's provenance -- all
      in one transaction.
    - Execution history is never deleted, modified, or used to block a
      planning edit: a deleted/replaced task or placement that executions
      reference simply leaves those executions with their historical
      task_id/scheduled_task_id and snapshot (see app/execution/db.py,
      "Execution <-> planning links"). PlacementReplacement.
      removed_with_history_ids reports such placements so a UI can say so.
    - Dependency cycles and overlapping fixed blocks are stored as given by
      the single-record API: detecting them is the PERT/constraint layers'
      job (allocation and the day scheduler already report them), not the
      store's. The bulk importers reject them.

Date-range task eligibility (tasks_for_range / load_range), derived from
app.planning.allocation._feasible_dates_for_task's hard date rules so a
range query never hides a task allocation could place there:
    - required_date set: eligible iff start <= required_date <= end;
    - else deadline set: eligible iff the deadline's UTC calendar date is
      >= start (some date in range is on/before the deadline);
    - else: always eligible (a floating task).
Recurrence is model-only (no expansion): a recurring template is treated
exactly like any other task. Placements and preferred_dates never affect
eligibility (preferred_dates only rank dates). A dependency outside the
eligible set is not pulled into the range; external_dependencies() resolves
its persisted state (completed / scheduled / skipped / cancelled / pending /
missing, see app/planning/external_dependencies.py) so allocation and the
day engine can treat it correctly. Ordering is (created_at, id),
deterministic across reopen.

RangeScope.PLANNED (used by the desktop pages, whose tasks are entered
"on" a date): a task's *planned date* is its required_date, else its
earliest preferred date, else none (task_planned_date). A range's planned
tasks are those whose planned date lies in the range, plus undated tasks
that are eligible for it. Unlike ELIGIBLE, a task planned for another week
does not appear in (or get allocated into) this week just because it has
no hard date constraint. ELIGIBLE remains the default for load_range.

clear_range(start, end, include_planning_data=...) is the "reset" scope:
it always deletes the placements (and schedule provenance) dated in the
range; with include_planning_data it also deletes the fixed blocks dated in
the range and the tasks whose planned date is in the range (undated tasks
are never touched). All in one transaction, subject to the deletion policy
above (e.g. refused if a task outside the range depends on one inside it).
Execution history is never deleted by it. A reset is a range operation on
whatever is stored inside one transaction; it writes no caller-supplied
record content, so it takes no per-record precondition.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from app.execution.db import get_connection
from app.pert import has_cycle_by_id
from app.planning.errors import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    InvalidEntityError,
    InvalidReferenceError,
    ScopeError,
    VersionConflictError,
)
from app.planning.external_dependencies import (
    ExternalDependency,
    external_dependency_ids,
    resolve_external_dependencies,
)
from app.planning.models import (
    DayScheduleOutput,
    FixedBlock,
    Project,
    ScheduledTask,
    Task,
    TaskRegistry,
    compute_total_score,
)
from app.planning.occurrence import HISTORY_PROTECTED_STATUSES, occurrence_key
from app.planning.preferences import OptimizerMode, PreferenceOverrides, PreferenceRecord, PreferenceScope
from app.planning.provenance import GenerationRecord, placements_digest
from app.planning.repository import PlanningRepository

Clock = Callable[[], datetime]

_AUDIT_FIELDS = {"created_at", "updated_at", "version"}


class RangeScope(str, Enum):
    #: Allocation feasibility (see "Date-range task eligibility").
    ELIGIBLE = "eligible"
    #: Planned date in range, plus undated eligible tasks (see RangeScope.PLANNED notes).
    PLANNED = "planned"


def task_planned_date(task: Task) -> date_ | None:
    """required_date, else the earliest preferred date, else None (mirrors the repository SQL)."""
    if task.required_date is not None:
        return task.required_date
    return min(task.preferred_dates) if task.preferred_dates else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dates(start_date: date_, end_date: date_) -> list[date_]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _require_range(start_date: date_, end_date: date_) -> None:
    if end_date < start_date:
        raise ScopeError(f"end_date {end_date} is before start_date {start_date}.")


def _group_by_date(items, start_date: date_, end_date: date_) -> dict:
    grouped = {day: [] for day in _dates(start_date, end_date)}
    for item in items:
        grouped[item.planned_date].append(item)
    return grouped


def _same_content(stored, incoming) -> bool:
    return stored.model_dump(exclude=_AUDIT_FIELDS) == incoming.model_dump(exclude=_AUDIT_FIELDS)


@dataclass(frozen=True)
class PlanningRange:
    """Everything persisted that planning needs for one inclusive date range (a snapshot)."""

    start_date: date_
    end_date: date_
    #: Eligible tasks (see the module docstring), in (created_at, id) order.
    task_ids: list[uuid.UUID]
    tasks: TaskRegistry
    #: Every date in the range is a key, even when it has no entries.
    fixed_blocks_by_date: dict[date_, list[FixedBlock]]
    placements_by_date: dict[date_, list[ScheduledTask]]


@dataclass(frozen=True)
class PlacementReplacement:
    start_date: date_
    end_date: date_
    #: The stored placements of the range after replacement, as re-read.
    placements: list[ScheduledTask]
    removed_ids: list[uuid.UUID] = field(default_factory=list)
    #: The removed placements that execution history references (history kept).
    removed_with_history_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class GenerationProvenance:
    """What a range generation was computed from (see app/planning/provenance.py)."""

    allocation_id: uuid.UUID
    range_start: date_
    range_end: date_
    range_scope: str
    fingerprint: str
    timezone: str
    engine_modes: Mapping[date_, OptimizerMode]
    generated_at: datetime


@dataclass(frozen=True)
class RescheduleResult:
    replacement: PlacementReplacement
    #: Active placements outside the range removed because a new placement superseded them.
    superseded_ids: list[uuid.UUID]
    #: Superseded placements outside the range kept because their execution had started or finished.
    history_protected_ids: list[uuid.UUID]
    generations: list[GenerationRecord]


@dataclass(frozen=True)
class ImportApplyResult:
    """What one committed import wrote (and, for a replace, cleared first)."""

    tasks: list[Task]
    fixed_blocks: list[FixedBlock]
    replaced_range: tuple[date_, date_] | None
    cleared: RangeClearResult | None


@dataclass(frozen=True)
class RangeClearResult:
    deleted_placements: int
    deleted_fixed_blocks: int
    deleted_tasks: int
    #: Deleted placements that execution history references (history kept).
    placements_with_history: int


@dataclass(frozen=True)
class RecordBatch:
    """Identity-bearing records to merge in one transaction (see apply_record_batch)."""

    projects: list[Project] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    fixed_blocks: list[FixedBlock] = field(default_factory=list)
    placements: list[ScheduledTask] = field(default_factory=list)


@dataclass(frozen=True)
class BatchApplyResult:
    #: Per kind ("project", "task", "fixed_block", "placement"): how many records were ...
    created: dict[str, int]
    updated: dict[str, int]
    deleted: dict[str, int]
    unchanged: dict[str, int]


# Per record kind: (repository table, human-readable name).
_KINDS = {
    "project": ("projects", "project"),
    "task": ("tasks", "task"),
    "fixed_block": ("fixed_blocks", "fixed block"),
    "placement": ("scheduled_tasks", "placement"),
}


class PlanningService:
    def __init__(self, repository: PlanningRepository, clock: Clock = _utcnow) -> None:
        self._repository = repository
        self._clock = clock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several service calls into one atomic unit of work."""
        with self._repository.transaction():
            yield

    # ------------------------------------------------------------------
    # Preconditions
    # ------------------------------------------------------------------

    def _check_version(self, kind: str, entity_id, expected_version: int) -> None:
        """Raise the structured error for a failed precondition on one stored record (live or tombstoned)."""
        table, label = _KINDS[kind]
        state = self._repository.record_states(table, [entity_id]).get(str(entity_id))
        if state is None:
            raise EntityNotFoundError(label, entity_id)
        version, deleted = state
        if deleted or version != expected_version:
            raise VersionConflictError(
                label, entity_id, expected_version=expected_version, current_version=version, deleted=deleted
            )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        """Persist a new task exactly as given. DuplicateEntityError if its id exists (even as a tombstone)."""
        return self.create_tasks([task])[0]

    def create_tasks(self, tasks: Iterable[Task]) -> list[Task]:
        with self._repository.transaction():
            return self._write_tasks(list(tasks), {})

    def update_task(self, task: Task, *, expected_version: int) -> Task:
        """
        Update a stored task if it is still at `expected_version` (see the
        concurrency rules). EntityNotFoundError if it never existed;
        VersionConflictError if it changed or was deleted meanwhile.
        """
        with self._repository.transaction():
            return self._write_tasks([task], {task.id: expected_version})[0]

    def save_task(self, task: Task, *, expected_version: int | None = None) -> Task:
        """Create (expected_version=None) or update one task."""
        if expected_version is None:
            return self.create_task(task)
        return self.update_task(task, expected_version=expected_version)

    def save_tasks(self, tasks: Iterable[Task], *, expected_versions: Mapping[uuid.UUID, int] | None = None) -> list[Task]:
        """
        Create or update several tasks atomically: a task whose id is in
        `expected_versions` is an update with that precondition, every other
        task is a create. Either every task (and every child row) is written,
        or none is. Dependencies/projects may reference other tasks in the
        same batch, in any order.
        """
        with self._repository.transaction():
            return self._write_tasks(list(tasks), dict(expected_versions or {}))

    def get_task(self, task_id: uuid.UUID) -> Task | None:
        return self._repository.get_task(task_id)

    def get_tasks(self, task_ids: Iterable[uuid.UUID]) -> TaskRegistry:
        """The persisted (live) subset of `task_ids`, as a registry."""
        return TaskRegistry(tasks=self._repository.get_tasks(task_ids))

    def list_tasks(self, *, include_deleted: bool = False) -> list[Task]:
        return self._repository.list_tasks(include_deleted=include_deleted)

    def tasks_for_range(self, start_date: date_, end_date: date_) -> list[Task]:
        """Tasks eligible for [start_date, end_date] (see the module docstring)."""
        _require_range(start_date, end_date)
        return self._repository.list_tasks_eligible_for_range(start_date, end_date)

    def tasks_planned_in_range(
        self, start_date: date_, end_date: date_, *, include_undated: bool = True, include_deleted: bool = False
    ) -> list[Task]:
        """Tasks in RangeScope.PLANNED for the range (or only dated ones)."""
        _require_range(start_date, end_date)
        return self._repository.list_tasks_planned_in_range(
            start_date, end_date, include_undated=include_undated, include_deleted=include_deleted
        )

    def delete_task(self, task_id: uuid.UUID, *, expected_version: int) -> bool:
        """Delete (tombstone) one task; False if it does not exist or is already deleted. See the deletion policy."""
        return self.delete_tasks({task_id: expected_version}) > 0

    def delete_tasks(self, expected_versions: Mapping[uuid.UUID, int]) -> int:
        """Delete several tasks atomically, each only if still at its expected version."""
        with self._repository.transaction():
            return self._delete_tasks(dict(expected_versions))

    def _delete_tasks(self, expected_versions: dict[uuid.UUID, int]) -> int:
        # Caller holds a repository transaction.
        ids = set(expected_versions)
        blocking = {
            dependency_id: dependents - ids
            for dependency_id, dependents in self._repository.dependents_of(ids).items()
            if dependents - ids
        }
        if blocking:
            dependents = set().union(*blocking.values())
            raise EntityInUseError(
                "Cannot delete task(s) "
                + ", ".join(sorted(str(task_id) for task_id in blocking))
                + " while other tasks depend on them: "
                + ", ".join(sorted(str(task_id) for task_id in dependents))
                + ". Remove those dependencies first, or delete the dependents too.",
                dependents,
            )

        states = self._repository.record_states("tasks", ids)
        live = []
        for task_id in sorted(ids, key=str):
            state = states.get(str(task_id))
            if state is None or state[1]:
                continue  # never existed, or already deleted: nothing to overwrite
            if state[0] != expected_versions[task_id]:
                raise VersionConflictError(
                    "task", task_id, expected_version=expected_versions[task_id], current_version=state[0]
                )
            live.append(task_id)

        now = self._clock()
        for task_id in live:
            if not self._repository.soft_delete_task(task_id, deleted_at=now, expected_version=expected_versions[task_id]):
                self._check_version("task", task_id, expected_versions[task_id])
        placements = self._repository.active_placements_for_tasks(live)
        self._repository.soft_delete_placements(
            (placement.id for group in placements.values() for placement in group), deleted_at=now
        )
        return len(live)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, project: Project) -> Project:
        if project.deleted_at is not None:
            raise InvalidEntityError("a project cannot be created already deleted; use delete_project.")
        with self._repository.transaction():
            self._repository.insert_project(project)
            return self._repository.get_project(project.id)

    def update_project(self, project: Project, *, expected_version: int) -> Project:
        if project.deleted_at is not None:
            raise InvalidEntityError("use delete_project to delete a project.")
        with self._repository.transaction():
            stored = self._repository.get_project(project.id)
            if stored is None or stored.version != expected_version:
                self._check_version("project", project.id, expected_version)
            if project.user_id != stored.user_id:
                raise InvalidEntityError(f"project {project.id}: its owner (user_id) cannot be changed by an update.")
            if _same_content(stored, project):
                return stored
            to_store = project.model_copy(
                update={"created_at": stored.created_at, "updated_at": self._clock(), "version": expected_version + 1}
            )
            if not self._repository.update_project(to_store, expected_version=expected_version):
                self._check_version("project", project.id, expected_version)
            return self._repository.get_project(project.id)

    def save_project(self, project: Project, *, expected_version: int | None = None) -> Project:
        if expected_version is None:
            return self.create_project(project)
        return self.update_project(project, expected_version=expected_version)

    def get_project(self, project_id: uuid.UUID) -> Project | None:
        return self._repository.get_project(project_id)

    def list_projects(self, *, include_deleted: bool = False) -> list[Project]:
        return self._repository.list_projects(include_deleted=include_deleted)

    def delete_project(self, project_id: uuid.UUID, *, expected_version: int) -> bool:
        with self._repository.transaction():
            task_ids = self._repository.task_ids_for_project(project_id)
            if task_ids:
                raise EntityInUseError(
                    f"Cannot delete project {project_id}: {len(task_ids)} task(s) still belong to it.", task_ids
                )
            state = self._repository.record_states("projects", [project_id]).get(str(project_id))
            if state is None or state[1]:
                return False
            if not self._repository.soft_delete_project(
                project_id, deleted_at=self._clock(), expected_version=expected_version
            ):
                self._check_version("project", project_id, expected_version)
            return True

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def create_fixed_block(self, block: FixedBlock) -> FixedBlock:
        if block.deleted_at is not None:
            raise InvalidEntityError("a fixed block cannot be created already deleted; use delete_fixed_block.")
        with self._repository.transaction():
            self._repository.insert_fixed_block(block)
            return self._repository.get_fixed_blocks([block.id])[block.id]

    def update_fixed_block(self, block: FixedBlock, *, expected_version: int) -> FixedBlock:
        """Update one fixed block (it may move to another date) if still at `expected_version`."""
        with self._repository.transaction():
            return self._update_fixed_block(block, expected_version)

    def _update_fixed_block(self, block: FixedBlock, expected_version: int) -> FixedBlock:
        if block.deleted_at is not None:
            raise InvalidEntityError("use delete_fixed_block to delete a fixed block.")
        stored = self._repository.get_fixed_blocks([block.id]).get(block.id)
        if stored is None or stored.version != expected_version:
            self._check_version("fixed_block", block.id, expected_version)
        if block.user_id != stored.user_id:
            raise InvalidEntityError(f"fixed block {block.id}: its owner (user_id) cannot be changed by an update.")
        if _same_content(stored, block):
            return stored
        to_store = block.model_copy(
            update={"created_at": stored.created_at, "updated_at": self._clock(), "version": expected_version + 1}
        )
        if not self._repository.update_fixed_block(to_store, expected_version=expected_version):
            self._check_version("fixed_block", block.id, expected_version)
        return self._repository.get_fixed_blocks([block.id])[block.id]

    def save_fixed_block(self, block: FixedBlock, *, expected_version: int | None = None) -> FixedBlock:
        """Create (expected_version=None) or update one fixed block."""
        if expected_version is None:
            return self.create_fixed_block(block)
        return self.update_fixed_block(block, expected_version=expected_version)

    def delete_fixed_block(self, block_id: uuid.UUID, *, expected_version: int) -> bool:
        """Delete (tombstone) one fixed block; False if it does not exist or is already deleted."""
        with self._repository.transaction():
            state = self._repository.record_states("fixed_blocks", [block_id]).get(str(block_id))
            if state is None or state[1]:
                return False
            if not self._repository.soft_delete_fixed_block(
                block_id, deleted_at=self._clock(), expected_version=expected_version
            ):
                self._check_version("fixed_block", block_id, expected_version)
            return True

    def set_fixed_blocks_for_date(
        self, day: date_, blocks: Iterable[FixedBlock], *, expected_versions: Mapping[uuid.UUID, int] | None = None
    ) -> list[FixedBlock]:
        """
        Make `day`'s stored fixed blocks exactly `blocks`, atomically. Every
        block must be dated `day`; a block id already stored on another date
        is rejected (ScopeError) rather than moved. Other dates are untouched.

        Precondition: `expected_versions` must name every block currently
        stored on `day` (whether it is kept, changed, or removed) with the
        version the caller read. A stored block the caller did not know
        about, or one that changed since, is a VersionConflictError -- so a
        concurrent edit or addition is never silently overwritten or deleted.
        """
        blocks = list(blocks)
        expected_versions = dict(expected_versions or {})
        ids = [block.id for block in blocks]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError(f"duplicate fixed block ids for {day}.")
        wrong_date = [block for block in blocks if block.planned_date != day]
        if wrong_date:
            raise ScopeError(f"fixed block {wrong_date[0].id} is dated {wrong_date[0].planned_date}, not {day}.")

        with self._repository.transaction():
            elsewhere = [
                stored for stored in self._repository.get_fixed_blocks(ids).values() if stored.planned_date != day
            ]
            if elsewhere:
                raise ScopeError(
                    f"fixed block {elsewhere[0].id} is stored on {elsewhere[0].planned_date}; "
                    f"set_fixed_blocks_for_date({day}) will not move it."
                )
            current = {block.id: block for block in self._repository.list_fixed_blocks(day, day)}
            for block_id, block in current.items():
                if block_id not in expected_versions:
                    raise VersionConflictError(
                        "fixed block", block_id, expected_version=None, current_version=block.version,
                        message=f"The fixed block {block.label!r} on {day} was added or changed by someone else. "
                        "Reload and try again.",
                    )
                if block.version != expected_versions[block_id]:
                    raise VersionConflictError(
                        "fixed block", block_id, expected_version=expected_versions[block_id],
                        current_version=block.version,
                    )
            for block_id, expected in expected_versions.items():
                if block_id not in current:
                    self._check_version("fixed_block", block_id, expected)

            now = self._clock()
            for block_id in set(current) - set(ids):
                self._repository.soft_delete_fixed_block(block_id, deleted_at=now, expected_version=current[block_id].version)
            for block in blocks:
                if block.id in current:
                    self._update_fixed_block(block, current[block.id].version)
                else:
                    if block.deleted_at is not None:
                        raise InvalidEntityError("a fixed block cannot be created already deleted.")
                    self._repository.insert_fixed_block(block)
            return self._repository.list_fixed_blocks(day, day)

    def fixed_blocks_for_date(self, day: date_) -> list[FixedBlock]:
        return self._repository.list_fixed_blocks(day, day)

    def fixed_blocks_for_range(self, start_date: date_, end_date: date_) -> dict[date_, list[FixedBlock]]:
        _require_range(start_date, end_date)
        return _group_by_date(self._repository.list_fixed_blocks(start_date, end_date), start_date, end_date)

    # ------------------------------------------------------------------
    # Placements
    # ------------------------------------------------------------------

    def replace_placements(
        self,
        start_date: date_,
        end_date: date_,
        placements: Iterable[ScheduledTask],
        *,
        expected_versions: Mapping[uuid.UUID, int] | None = None,
    ) -> PlacementReplacement:
        """
        Atomically make the live placements dated within [start_date,
        end_date] exactly `placements` (see the module docstring's
        replacement policy). With `expected_versions`, the range's current
        live placements must be exactly those ids at those versions (else
        VersionConflictError). Any failure leaves every stored placement, in
        and out of the range, unchanged.
        """
        _require_range(start_date, end_date)
        with self._repository.transaction():
            return self._replace_range(start_date, end_date, list(placements), expected_versions)

    def _replace_range(
        self,
        start_date: date_,
        end_date: date_,
        placements: list[ScheduledTask],
        expected_versions: Mapping[uuid.UUID, int] | None,
    ) -> PlacementReplacement:
        # Caller holds a repository transaction.
        ids = [placement.id for placement in placements]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError("duplicate placement ids in replacement.")
        outside = [p for p in placements if not start_date <= p.planned_date <= end_date]
        if outside:
            raise ScopeError(
                f"placement {outside[0].id} is dated {outside[0].planned_date}, outside [{start_date}, {end_date}]."
            )

        stored_by_id = self._repository.get_placements(ids)
        moved = [stored for stored in stored_by_id.values() if not start_date <= stored.planned_date <= end_date]
        if moved:
            raise ScopeError(
                f"placement {moved[0].id} is stored on {moved[0].planned_date}, outside "
                f"[{start_date}, {end_date}]; a scoped replacement will not move it."
            )
        tombstoned = [
            placement_id for placement_id, (_, deleted) in self._repository.record_states("scheduled_tasks", ids).items()
            if deleted
        ]
        if tombstoned:
            raise DuplicateEntityError("placement", uuid.UUID(sorted(tombstoned)[0]))

        task_ids = {placement.task_id for placement in placements}
        tasks = self._repository.get_tasks(task_ids)
        missing = task_ids - set(tasks)
        if missing:
            raise InvalidReferenceError(
                "placements reference tasks that are not persisted: " + ", ".join(sorted(map(str, missing))),
                missing,
            )

        current = self._repository.list_placements(start_date, end_date)
        if expected_versions is not None:
            current_versions = {placement.id: placement.version for placement in current}
            expected = dict(expected_versions)
            for placement_id in sorted(set(current_versions) | set(expected), key=str):
                if current_versions.get(placement_id) != expected.get(placement_id):
                    raise VersionConflictError(
                        "placement", placement_id,
                        expected_version=expected.get(placement_id), current_version=current_versions.get(placement_id),
                        message=f"The saved schedule for {start_date} .. {end_date} changed while it was being "
                        "regenerated. Nothing was saved; run it again.",
                    )

        current_ids = {placement.id for placement in current}
        removed = current_ids - set(ids)
        removed_with_history = self._repository.placement_ids_with_history(removed)
        now = self._clock()
        self._repository.soft_delete_placements(removed, deleted_at=now)

        for placement in placements:
            placement = placement.model_copy(update={"user_id": tasks[placement.task_id].user_id, "deleted_at": None})
            stored = stored_by_id.get(placement.id)
            if stored is None:
                self._repository.insert_placement(placement)
                continue
            if _same_content(stored, placement):
                continue
            to_store = placement.model_copy(
                update={"created_at": stored.created_at, "updated_at": now, "version": stored.version + 1}
            )
            if not self._repository.update_placement(to_store, expected_version=stored.version):
                self._check_version("placement", placement.id, stored.version)

        return PlacementReplacement(
            start_date=start_date,
            end_date=end_date,
            placements=self._repository.list_placements(start_date, end_date),
            removed_ids=sorted(removed, key=str),
            removed_with_history_ids=sorted(removed_with_history, key=str),
        )

    def reschedule_range(
        self,
        start_date: date_,
        end_date: date_,
        outputs: Mapping[date_, DayScheduleOutput],
        *,
        expected_versions: Mapping[uuid.UUID, int],
        provenance: GenerationProvenance,
    ) -> RescheduleResult:
        """
        Make Schedule's save, in one transaction:

        1. replace the range's placements with every output's placements,
           provided the range's live placements are still exactly
           `expected_versions` (the ones the generation read as its previous
           result) -- otherwise VersionConflictError and nothing changes;
        2. remove (tombstone) active placements *outside* the range that a
           new placement supersedes (same occurrence key, see
           app/planning/occurrence.py), except history-protected ones whose
           execution has started or finished;
        3. save one GenerationRecord per date of the range -- including
           dates whose successful result placed nothing -- with the inputs
           fingerprint and the committed placements' digest.

        `outputs` must contain exactly the dates of [start_date, end_date].
        """
        _require_range(start_date, end_date)
        dates = _dates(start_date, end_date)
        if set(outputs) != set(dates):
            raise ScopeError(f"reschedule_range needs exactly one output per date of [{start_date}, {end_date}].")
        if not provenance.range_start <= start_date <= end_date <= provenance.range_end:
            raise ScopeError("the provenance range must contain the rescheduled range.")

        placements = [placement for day in dates for placement in outputs[day].placements]
        with self._repository.transaction():
            replacement = self._replace_range(start_date, end_date, placements, expected_versions)
            now = self._clock()

            new_placements = replacement.placements
            tasks = self._repository.get_tasks({placement.task_id for placement in new_placements})
            new_keys = {occurrence_key(placement, tasks[placement.task_id]) for placement in new_placements}
            candidates = [
                placement
                for group in self._repository.active_placements_for_tasks(tasks).values()
                for placement in group
                if not start_date <= placement.planned_date <= end_date
                and occurrence_key(placement, tasks[placement.task_id]) in new_keys
            ]
            statuses = self._repository.placement_execution_statuses(placement.id for placement in candidates)
            protected = [p.id for p in candidates if statuses.get(p.id) in HISTORY_PROTECTED_STATUSES]
            superseded = [p.id for p in candidates if statuses.get(p.id) not in HISTORY_PROTECTED_STATUSES]
            self._repository.soft_delete_placements(superseded, deleted_at=now)

            existing = {record.planned_date: record for record in self._repository.list_generations(start_date, end_date)}
            generations: list[GenerationRecord] = []
            for day in dates:
                output = outputs[day]
                saved = self._repository.list_placements(day, day)
                fields = dict(
                    planned_date=day,
                    timezone=provenance.timezone,
                    engine_mode=provenance.engine_modes[day],
                    range_start=provenance.range_start,
                    range_end=provenance.range_end,
                    range_scope=provenance.range_scope,
                    allocation_id=provenance.allocation_id,
                    fingerprint=provenance.fingerprint,
                    placements_digest=placements_digest(saved),
                    placement_count=len(saved),
                    unscheduled_count=len(output.unscheduled),
                    total_score=compute_total_score(saved),
                    generated_at=provenance.generated_at,
                )
                previous = existing.get(day)
                if previous is None:
                    record = GenerationRecord(**fields, created_at=now, updated_at=now)
                    self._repository.insert_generation(record)
                else:
                    record = previous.model_copy(update={**fields, "updated_at": now, "version": previous.version + 1})
                    if not self._repository.update_generation(record, expected_version=previous.version):
                        raise VersionConflictError(
                            "schedule record", day, expected_version=previous.version, current_version=None,
                            message=f"The saved schedule of {day} changed while it was being regenerated. "
                            "Nothing was saved; run it again.",
                        )
                generations.append(record)

            return RescheduleResult(
                replacement=replacement,
                superseded_ids=sorted(superseded, key=str),
                history_protected_ids=sorted(protected, key=str),
                generations=generations,
            )

    def placements_for_date(self, day: date_) -> list[ScheduledTask]:
        return self._repository.list_placements(day, day)

    def placements_for_range(self, start_date: date_, end_date: date_) -> dict[date_, list[ScheduledTask]]:
        _require_range(start_date, end_date)
        return _group_by_date(self._repository.list_placements(start_date, end_date), start_date, end_date)

    def stored_day_output(self, day: date_, timezone_name: str) -> DayScheduleOutput | None:
        """
        The stored placements for `day` as a DayScheduleOutput (None if
        there are none) -- suitable as generate_selected_day's
        previous_result, so an unchanged placement keeps its id (and any
        execution history linked to it) across regenerations and restarts.
        """
        placements = self.placements_for_date(day)
        if not placements:
            return None
        registry = self.get_tasks(placement.task_id for placement in placements)
        return DayScheduleOutput(
            date=day, timezone=timezone_name, tasks=registry, placements=placements,
            total_score=compute_total_score(placements),
        )

    # ------------------------------------------------------------------
    # Schedule provenance
    # ------------------------------------------------------------------

    def generation_record(self, day: date_) -> GenerationRecord | None:
        records = self._repository.list_generations(day, day)
        return records[0] if records else None

    def generation_records(self, start_date: date_, end_date: date_) -> dict[date_, GenerationRecord]:
        _require_range(start_date, end_date)
        return {record.planned_date: record for record in self._repository.list_generations(start_date, end_date)}

    # ------------------------------------------------------------------
    # Persisted preference layers
    # ------------------------------------------------------------------

    def user_preferences(self) -> PreferenceRecord | None:
        """The persisted user-level layer (between YAML and the date layers), if any."""
        return self._repository.get_preference(PreferenceScope.USER)

    def date_preferences(self, day: date_) -> PreferenceRecord | None:
        return self._repository.get_preference(PreferenceScope.DATE, day)

    def date_preferences_for_range(self, start_date: date_, end_date: date_) -> dict[date_, PreferenceRecord]:
        _require_range(start_date, end_date)
        return {record.date: record for record in self._repository.list_date_preferences(start_date, end_date)}

    def save_user_preferences(
        self, overrides: PreferenceOverrides, *, expected_version: int | None = None
    ) -> PreferenceRecord:
        """Create (expected_version=None; refused if a layer exists) or update the user layer."""
        return self._save_preference(PreferenceScope.USER, None, overrides, expected_version)

    def save_date_preferences(
        self, day: date_, overrides: PreferenceOverrides, *, expected_version: int | None = None
    ) -> PreferenceRecord:
        """Create (expected_version=None; refused if a layer exists) or update `day`'s layer."""
        return self._save_preference(PreferenceScope.DATE, day, overrides, expected_version)

    def delete_user_preferences(self, *, expected_version: int) -> bool:
        return self._delete_preference(PreferenceScope.USER, None, expected_version)

    def delete_date_preferences(self, day: date_, *, expected_version: int) -> bool:
        return self._delete_preference(PreferenceScope.DATE, day, expected_version)

    def _save_preference(
        self, scope: PreferenceScope, day: date_ | None, overrides: PreferenceOverrides, expected_version: int | None
    ) -> PreferenceRecord:
        overrides = PreferenceOverrides.model_validate(overrides.model_dump())  # an independent copy
        with self._repository.transaction():
            stored = self._repository.get_preference(scope, day)
            label = "user preferences" if day is None else f"preferences for {day}"
            if expected_version is None:
                if stored is not None:
                    raise VersionConflictError(
                        "preference", stored.id, expected_version=None, current_version=stored.version,
                        message=f"The {label} already exist (version {stored.version}); reload them and pass "
                        "their version to change them.",
                    )
                now = self._clock()
                record = PreferenceRecord(scope=scope, date=day, overrides=overrides, created_at=now, updated_at=now)
                self._repository.insert_preference(record)
                return self._repository.get_preference(scope, day)

            if stored is None or stored.version != expected_version:
                raise VersionConflictError(
                    "preference", stored.id if stored else label, expected_version=expected_version,
                    current_version=stored.version if stored else None, deleted=stored is None,
                )
            if stored.overrides == overrides:
                return stored
            record = stored.model_copy(
                update={"overrides": overrides, "updated_at": self._clock(), "version": expected_version + 1}
            )
            if not self._repository.update_preference(record, expected_version=expected_version):
                raise VersionConflictError(
                    "preference", stored.id, expected_version=expected_version, current_version=None
                )
            return self._repository.get_preference(scope, day)

    def _delete_preference(self, scope: PreferenceScope, day: date_ | None, expected_version: int) -> bool:
        with self._repository.transaction():
            stored = self._repository.get_preference(scope, day)
            if stored is None:
                return False
            if stored.version != expected_version or not self._repository.soft_delete_preference(
                stored.id, deleted_at=self._clock(), expected_version=expected_version
            ):
                raise VersionConflictError(
                    "preference", stored.id, expected_version=expected_version, current_version=stored.version
                )
            return True

    # ------------------------------------------------------------------
    # Range loading
    # ------------------------------------------------------------------

    def load_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.ELIGIBLE
    ) -> PlanningRange:
        """One consistent snapshot of the range's tasks (per `scope`), fixed blocks, and placements."""
        _require_range(start_date, end_date)
        with self._repository.transaction():
            if scope == RangeScope.PLANNED:
                tasks = self._repository.list_tasks_planned_in_range(start_date, end_date)
            else:
                tasks = self._repository.list_tasks_eligible_for_range(start_date, end_date)
            fixed_blocks = self._repository.list_fixed_blocks(start_date, end_date)
            placements = self._repository.list_placements(start_date, end_date)

        return PlanningRange(
            start_date=start_date,
            end_date=end_date,
            task_ids=[task.id for task in tasks],
            tasks=TaskRegistry(tasks={task.id: task for task in tasks}),
            fixed_blocks_by_date=_group_by_date(fixed_blocks, start_date, end_date),
            placements_by_date=_group_by_date(placements, start_date, end_date),
        )

    def external_dependencies(
        self, tasks: Iterable[Task], start_date: date_, end_date: date_, timezone_name: str
    ) -> dict[uuid.UUID, ExternalDependency]:
        """
        The persisted state of every dependency of `tasks` that is not itself
        among `tasks` (see app/planning/external_dependencies.py): read from
        the stored tasks, their active placements outside the range, and
        their execution history, in one consistent snapshot.
        """
        _require_range(start_date, end_date)
        dependency_ids = external_dependency_ids(tasks)
        if not dependency_ids:
            return {}
        with self._repository.transaction():
            persisted = self._repository.existing_task_ids(dependency_ids)
            placements = self._repository.active_placements_for_tasks(persisted)
            executions = self._repository.execution_facts_for_tasks(persisted)
        return resolve_external_dependencies(
            dependency_ids,
            range_start=start_date,
            range_end=end_date,
            timezone_name=timezone_name,
            persisted_task_ids=persisted,
            placements_by_task=placements,
            executions_by_task=executions,
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def clear_range(self, start_date: date_, end_date: date_, *, include_planning_data: bool) -> RangeClearResult:
        """
        Delete the placements (and schedule provenance) dated in the range
        and, with include_planning_data, the range's fixed blocks and dated
        tasks -- atomically (see the module docstring). Never touches
        execution history or anything dated outside the range.
        """
        _require_range(start_date, end_date)
        with self._repository.transaction():
            now = self._clock()
            placement_ids = [p.id for p in self._repository.list_placements(start_date, end_date)]
            history = self._repository.placement_ids_with_history(placement_ids)
            self._repository.soft_delete_placements(placement_ids, deleted_at=now)
            self._repository.soft_delete_generations(start_date, end_date, deleted_at=now)

            block_ids: list[uuid.UUID] = []
            deleted_tasks = 0
            if include_planning_data:
                blocks = self._repository.list_fixed_blocks(start_date, end_date)
                block_ids = [block.id for block in blocks]
                for block in blocks:
                    self._repository.soft_delete_fixed_block(block.id, deleted_at=now, expected_version=block.version)
                tasks = self._repository.list_tasks_planned_in_range(start_date, end_date, include_undated=False)
                # Their placements dated *outside* the range go with them.
                deleted_tasks = self._delete_tasks({task.id: task.version for task in tasks})

            return RangeClearResult(
                deleted_placements=len(placement_ids),
                deleted_fixed_blocks=len(block_ids),
                deleted_tasks=deleted_tasks,
                placements_with_history=len(history),
            )

    # ------------------------------------------------------------------
    # Bulk import
    # ------------------------------------------------------------------

    def apply_import(
        self,
        tasks: Iterable[Task],
        fixed_blocks: Iterable[FixedBlock],
        *,
        replace_range: tuple[date_, date_] | None = None,
    ) -> ImportApplyResult:
        """
        Write a validated legacy import in one transaction: optionally clear
        `replace_range` first (clear_range with planning data), then create
        every task and fixed block. New entities only -- an id that is
        already stored is a DuplicateEntityError, and a fixed block that
        overlaps a stored block on its date (after any clearing) is an
        InvalidEntityError. Any failure rolls back everything, including
        the clearing.
        """
        tasks = list(tasks)
        fixed_blocks = list(fixed_blocks)
        with self._repository.transaction():
            cleared = None
            if replace_range is not None:
                cleared = self.clear_range(*replace_range, include_planning_data=True)

            existing_tasks = self._repository.existing_task_ids((task.id for task in tasks), include_deleted=True)
            if existing_tasks:
                raise DuplicateEntityError("task", sorted(existing_tasks, key=str)[0])
            existing_blocks = self._repository.record_states("fixed_blocks", (block.id for block in fixed_blocks))
            if existing_blocks:
                raise DuplicateEntityError("fixed block", uuid.UUID(sorted(existing_blocks)[0]))

            for block in fixed_blocks:
                for stored in self._repository.list_fixed_blocks(block.planned_date, block.planned_date):
                    if block.planned_start < stored.planned_end and stored.planned_start < block.planned_end:
                        raise InvalidEntityError(
                            f"fixed block {block.label!r} overlaps the saved fixed block {stored.label!r} "
                            f"on {block.planned_date}."
                        )
                self._repository.insert_fixed_block(block)

            saved_tasks = self._write_tasks(tasks, {}) if tasks else []
            saved_blocks = self._repository.get_fixed_blocks(block.id for block in fixed_blocks)
            return ImportApplyResult(
                tasks=saved_tasks,
                fixed_blocks=[saved_blocks[block.id] for block in fixed_blocks],
                replaced_range=replace_range,
                cleared=cleared,
            )

    def apply_record_batch(self, batch: RecordBatch, *, allow_updates: bool = False) -> BatchApplyResult:
        """
        Merge identity-bearing records (e.g. a canonical planning CSV, see
        app/planning/csv_canonical.py) in one transaction. Deterministic
        collision rules, per record, against what is stored (live or
        tombstoned) under the same id:

            - not stored: created exactly as given (a record that arrives
              already deleted is stored as a tombstone, so deletion metadata
              round-trips);
            - stored with identical content and deletion state: a no-op,
              whatever the versions say; likewise a record that arrives
              deleted when it is already deleted here (the stored
              tombstone stands);
            - stored as a tombstone but arriving live: rejected (an import
              never revives a deleted record);
            - otherwise the record diverges from the stored one. Without
              allow_updates that is rejected. With allow_updates the
              record's own `version` is its precondition: it must equal the
              stored version (the version it was exported at), else
              VersionConflictError. It is then applied as one logical
              mutation (stored version + 1, updated_at = now): an update, or
              a soft delete when it arrives deleted. A version value in the
              batch can therefore never jump ahead of, or overwrite, newer
              stored data.

        The batch must have a single owner (user_id) that matches every
        stored record it touches or references. After applying, the complete
        result is validated -- relationships (project, dependency and
        placement task references must point at live records; a deleted
        task/project must have no live dependents/tasks), dependency cycles,
        overlapping fixed blocks -- and any problem rolls back everything.
        """
        records = {
            "project": list(batch.projects),
            "task": list(batch.tasks),
            "fixed_block": list(batch.fixed_blocks),
            "placement": list(batch.placements),
        }
        for kind, items in records.items():
            ids = [item.id for item in items]
            if len(set(ids)) != len(ids):
                raise InvalidEntityError(f"the batch contains the same {_KINDS[kind][1]} id more than once.")
        owners = {item.user_id for items in records.values() for item in items}
        if len(owners) > 1:
            raise InvalidEntityError("the batch mixes records of different owners (user_id); import them separately.")
        owner = next(iter(owners), None)

        counts = {name: dict.fromkeys(records, 0) for name in ("created", "updated", "deleted", "unchanged")}
        with self._repository.transaction():
            now = self._clock()
            plans: dict[str, list[tuple[str, object, object]]] = {kind: [] for kind in records}
            for kind, items in records.items():
                stored_by_id = self._stored_for_batch(kind, [item.id for item in items])
                for item in items:
                    plans[kind].append(self._plan_batch_record(kind, item, stored_by_id.get(item.id), owner, allow_updates))

            self._check_batch_references(records, plans, owner)

            deleted_task_ids: list[uuid.UUID] = []
            touched_dates: set[date_] = set()
            for kind in ("project", "task", "fixed_block", "placement"):
                for action, item, stored in plans[kind]:
                    if kind in ("fixed_block", "placement"):
                        touched_dates.add(item.planned_date)
                        if stored is not None:
                            touched_dates.add(stored.planned_date)
                    if action == "unchanged":
                        counts["unchanged"][kind] += 1
                        continue
                    self._apply_batch_record(kind, action, item, stored, now)
                    counts[{"insert": "created", "update": "updated", "delete": "deleted"}[action]][kind] += 1
                    if kind == "task" and action == "delete":
                        deleted_task_ids.append(item.id)

            # A task deleted by the batch takes its remaining active placements with it.
            leftover = self._repository.active_placements_for_tasks(deleted_task_ids)
            self._repository.soft_delete_placements(
                (placement.id for group in leftover.values() for placement in group), deleted_at=now
            )
            self._validate_batch_result(plans, touched_dates)

        return BatchApplyResult(**counts)

    def _stored_for_batch(self, kind: str, ids: list[uuid.UUID]) -> dict:
        if kind == "project":
            wanted = set(ids)
            projects = self._repository.list_projects(include_deleted=True)
            return {project.id: project for project in projects if project.id in wanted}
        if kind == "task":
            return self._repository.get_tasks(ids, include_deleted=True)
        if kind == "fixed_block":
            return self._repository.get_fixed_blocks(ids, include_deleted=True)
        return self._repository.get_placements(ids, include_deleted=True)

    @staticmethod
    def _plan_batch_record(kind: str, item, stored, owner, allow_updates: bool) -> tuple[str, object, object]:
        label = _KINDS[kind][1]
        if stored is None:
            return ("insert", item, None)
        if stored.user_id != owner:
            raise InvalidEntityError(f"{label} {item.id} is stored for a different owner (user_id); it cannot be imported.")
        if _same_content(stored, item):
            return ("unchanged", item, stored)
        if stored.deleted_at is not None and item.deleted_at is not None:
            return ("unchanged", item, stored)  # deleted here and there: the stored tombstone stands
        if stored.deleted_at is not None:
            raise VersionConflictError(
                label, item.id, expected_version=item.version, current_version=stored.version, deleted=True,
                message=f"The {label} {item.id} has been deleted here; an import cannot bring it back.",
            )
        if not allow_updates:
            raise VersionConflictError(
                label, item.id, expected_version=item.version, current_version=stored.version,
                message=f"The {label} {item.id} already exists with different content (stored version "
                f"{stored.version}). Nothing was imported; allow updates to apply changes to existing records.",
            )
        if item.version != stored.version:
            raise VersionConflictError(label, item.id, expected_version=item.version, current_version=stored.version)
        if kind == "placement" and item.task_id != stored.task_id:
            raise InvalidEntityError(f"placement {item.id} cannot be moved to another task.")
        return ("delete" if item.deleted_at is not None else "update", item, stored)

    def _check_batch_references(self, records: dict, plans: dict, owner) -> None:
        """Every reference from a live batch record must point at a record that is live after the batch."""

        def final_live(kind: str, ids: set) -> set:
            in_batch = {item.id for _, item, _ in plans[kind]}
            batch_live = {item.id for _, item, _ in plans[kind] if item.deleted_at is None}
            stored_ids = ids - in_batch
            table, _ = _KINDS[kind]
            states = self._repository.record_states(table, stored_ids)
            stored_live = {uuid.UUID(key) for key, (_, deleted) in states.items() if not deleted}
            if stored_live:
                stored = (
                    self._repository.get_tasks(stored_live) if kind == "task"
                    else {p.id: p for p in self._repository.list_projects() if p.id in stored_live}
                )
                wrong_owner = [record_id for record_id, record in stored.items() if record.user_id != owner]
                if wrong_owner:
                    raise InvalidEntityError(
                        f"the batch references {_KINDS[kind][1]} {wrong_owner[0]}, which belongs to a different owner."
                    )
            return (ids & batch_live) | stored_live

        live_tasks = [item for action, item, _ in plans["task"] if item.deleted_at is None]
        live_placements = [item for action, item, _ in plans["placement"] if item.deleted_at is None]

        project_refs = {task.project_id for task in live_tasks if task.project_id is not None}
        missing_projects = project_refs - final_live("project", project_refs)
        if missing_projects:
            raise InvalidReferenceError(
                "project_id references projects that do not exist (in the batch or stored): "
                + ", ".join(sorted(map(str, missing_projects))),
                missing_projects,
            )

        task_refs = {dependency for task in live_tasks for dependency in task.dependency_ids}
        task_refs |= {placement.task_id for placement in live_placements}
        missing_tasks = task_refs - final_live("task", task_refs)
        if missing_tasks:
            raise InvalidReferenceError(
                "dependency/placement task references point at tasks that do not exist (in the batch or stored): "
                + ", ".join(sorted(map(str, missing_tasks))),
                missing_tasks,
            )

        batch_tasks = {item.id: item for _, item, _ in plans["task"]}
        for placement in (item for _, item, _ in plans["placement"]):
            task = batch_tasks.get(placement.task_id) or self._repository.get_task(placement.task_id, include_deleted=True)
            if task is not None and task.user_id != placement.user_id:
                raise InvalidEntityError(f"placement {placement.id} must have the same owner as its task.")

    def _apply_batch_record(self, kind: str, action: str, item, stored, now: datetime) -> None:
        table, label = _KINDS[kind]
        insert = {
            "project": self._repository.insert_project,
            "task": self._repository.insert_task,
            "fixed_block": self._repository.insert_fixed_block,
            "placement": self._repository.insert_placement,
        }[kind]
        update = {
            "project": self._repository.update_project,
            "task": self._repository.update_task,
            "fixed_block": self._repository.update_fixed_block,
            "placement": self._repository.update_placement,
        }[kind]
        if action == "insert":
            insert(item)
            return
        # A delete writes the stored record as a tombstone; both go through the live-row compare-and-update.
        base = stored if action == "delete" else item
        to_store = base.model_copy(
            update={
                "created_at": stored.created_at,
                "updated_at": now,
                "version": stored.version + 1,
                "deleted_at": now if action == "delete" else None,
            }
        )
        if not update(to_store, expected_version=stored.version):
            self._check_version(kind, item.id, stored.version)
            raise VersionConflictError(label, item.id, expected_version=stored.version, current_version=None)

    def _validate_batch_result(self, plans: dict, touched_dates: set[date_]) -> None:
        deleted_tasks = [item.id for action, item, _ in plans["task"] if action == "delete" or (
            action == "insert" and item.deleted_at is not None)]
        blockers = {
            dependency: dependents
            for dependency, dependents in self._repository.dependents_of(deleted_tasks).items()
            if dependents
        }
        if blockers:
            dependents = set().union(*blockers.values())
            raise EntityInUseError(
                "the batch deletes task(s) that live tasks still depend on: "
                + ", ".join(sorted(map(str, dependents))),
                dependents,
            )
        for action, project, _ in plans["project"]:
            if project.deleted_at is not None and self._repository.task_ids_for_project(project.id):
                raise EntityInUseError(f"the batch deletes project {project.id}, which still has live tasks.")

        if plans["task"] and has_cycle_by_id({task.id: task for task in self._repository.list_tasks()}):
            raise InvalidEntityError("the batch would create a dependency cycle among the stored tasks.")

        for day in sorted(touched_dates):
            ordered = sorted(self._repository.list_fixed_blocks(day, day), key=lambda block: block.planned_start)
            for earlier, later in zip(ordered, ordered[1:]):
                if later.planned_start < earlier.planned_end:
                    raise InvalidEntityError(
                        f"fixed block {later.label!r} would overlap fixed block {earlier.label!r} on {day}."
                    )

    # ------------------------------------------------------------------
    # Flat reads (exports)
    # ------------------------------------------------------------------

    def list_fixed_blocks(
        self, start_date: date_ = date_.min, end_date: date_ = date_.max, *, include_deleted: bool = False
    ) -> list[FixedBlock]:
        """Fixed blocks dated in the range (default: all), ordered by (date, start, id)."""
        _require_range(start_date, end_date)
        return self._repository.list_fixed_blocks(start_date, end_date, include_deleted=include_deleted)

    def list_placements(
        self, start_date: date_ = date_.min, end_date: date_ = date_.max, *, include_deleted: bool = False
    ) -> list[ScheduledTask]:
        """Placements dated in the range (default: all), ordered by (date, start, id)."""
        _require_range(start_date, end_date)
        return self._repository.list_placements(start_date, end_date, include_deleted=include_deleted)

    def get_tasks_including_deleted(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Task]:
        return self._repository.get_tasks(task_ids, include_deleted=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_tasks(self, tasks: list[Task], expected_versions: dict[uuid.UUID, int]) -> list[Task]:
        # Caller holds a repository transaction. Everything is validated before anything is written.
        ids = [task.id for task in tasks]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError("duplicate task ids in one save.")
        if any(task.deleted_at is not None for task in tasks):
            raise InvalidEntityError("use delete_task to delete a task; a save cannot set deleted_at.")
        batch_ids = set(ids)

        dependency_ids = {dependency for task in tasks for dependency in task.dependency_ids} - batch_ids
        missing_dependencies = dependency_ids - self._repository.existing_task_ids(dependency_ids)
        if missing_dependencies:
            raise InvalidReferenceError(
                "dependency_ids reference tasks that are not persisted: "
                + ", ".join(sorted(map(str, missing_dependencies))),
                missing_dependencies,
            )

        project_ids = {task.project_id for task in tasks if task.project_id is not None}
        missing_projects = project_ids - self._repository.existing_project_ids(project_ids)
        if missing_projects:
            raise InvalidReferenceError(
                "project_id references projects that are not persisted: " + ", ".join(sorted(map(str, missing_projects))),
                missing_projects,
            )

        states = self._repository.record_states("tasks", ids)
        stored_by_id = self._repository.get_tasks(expected_versions)
        for task in tasks:
            if task.id not in expected_versions:
                if str(task.id) in states:
                    raise DuplicateEntityError("task", task.id)
                continue
            stored = stored_by_id.get(task.id)
            if stored is None or stored.version != expected_versions[task.id]:
                self._check_version("task", task.id, expected_versions[task.id])
            if task.user_id != stored.user_id:
                raise InvalidEntityError(f"task {task.id}: its owner (user_id) cannot be changed by an update.")

        now = self._clock()
        for task in tasks:
            if task.id not in expected_versions:
                self._repository.insert_task(task)
                continue
            stored = stored_by_id[task.id]
            if _same_content(stored, task):
                continue
            expected = expected_versions[task.id]
            to_store = task.model_copy(update={"created_at": stored.created_at, "updated_at": now, "version": expected + 1})
            if not self._repository.update_task(to_store, expected_version=expected):
                self._check_version("task", task.id, expected)

        saved = self._repository.get_tasks(ids)
        return [saved[task_id] for task_id in ids]


@contextmanager
def open_planning_service(db_path: str | Path | None = None) -> Iterator[PlanningService]:
    """
    Open the application database and yield a PlanningService on it,
    closing the connection on exit. Pass an explicit db_path in tests;
    omit it to use the configured default location.
    """
    connection = get_connection(db_path)
    try:
        yield PlanningService(PlanningRepository(connection))
    finally:
        connection.close()
