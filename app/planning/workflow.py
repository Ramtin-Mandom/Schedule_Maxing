"""
app/planning/workflow.py

The shared, Tk-free scheduling workflow (Milestone 4): the one orchestration
of allocation, selected-day generation, saving and freshness that the
desktop PlanningController, the hosted web API (PostgreSQL, backend/) and
the local web API (SQLite) all run. It works on any PlanningService --
the storage lives behind the service's repository (the SQLite
PlanningRepository, or the server's SQLAlchemy repository) -- so there is
one allocator (app/planning/allocation.py), one day engine
(app/optimizer.py via app/planning/service.py), one preference resolution
(PlanningService.resolve_preferences), one set of occurrence rules
(app/planning/occurrence.py) and one provenance recipe
(app/planning/provenance.py) for every client. Nothing here holds state
between calls: an allocation preview is recomputed from persisted inputs
and identified by their fingerprint, never kept in a process.

Reading inputs (read_inputs): one consistent snapshot of a range's tasks
(per RangeScope, deadlines judged in the planning timezone), fixed blocks,
effective preferences and external dependencies, plus their fingerprint.
preview_allocation allocates such a snapshot without saving anything;
preference_views shows each date's effective and inherited preferences with
the stored layers they come from.

Generation (generate) for a contiguous run of dates inside an allocation
range, in one of two modes:

    FULL (the default; the canonical full generation, unchanged): each date
        is generated from scratch by the day engine, with the date's saved
        placements as previous_result -- an identical placement keeps its id,
        nothing else is locked -- and the result replaces the date's saved
        placements (PlanningService.reschedule_range: superseded occurrences
        outside the range are removed unless their execution has started or
        finished). With protect_history=True (the web API's behavior) work
        whose execution has started or finished is never moved or
        duplicated: such a placement on a generated date is kept as it is and
        reserved, and a task whose occurrence already has such a placement
        elsewhere is not placed again.

    INCREMENTAL (explicit opt-in): every saved placement of a generated date
        is kept exactly (id, interval, version, execution links) as long as
        it still fits the current inputs, and only newly eligible work -- tasks
        allocated to the date that have no live placement of their occurrence
        anywhere -- is scheduled into the time around it. Kept placements are
        passed to the engine as in-memory reserved intervals only; nothing
        fake is ever stored. If a kept placement no longer fits (its task was
        deleted, moved, re-timed, or its date's window, fixed blocks, engine
        mode, deadline or dependencies changed), RegenerationRequiredError says
        which and why, and nothing is saved: replacing kept work always takes
        an explicit FULL regeneration.

Every generation first checks freshness: when every requested date's saved
schedule is current for the current inputs, it returns already_current and
writes nothing (no id, version, timestamp, provenance or change-capture
change). Otherwise the engine runs outside any write transaction; then, in
one transaction, the inputs are read again and their fingerprint compared
with the one generation used (StaleInputsError if anything changed --
tasks, blocks, preferences, external dependencies), the saved placements'
versions are compared (VersionConflictError), and only then are placements
and provenance written together. A failure anywhere leaves the previous
schedule exactly as it was. A MandatoryTaskSchedulingError from the engine
is raised unchanged, with its per-task reasons.

Explicit rescheduling (reschedule_placement): moves one saved placement to
a new interval after validating the destination with the same hard rules a
kept placement must meet (reschedule_problems). It is not a generation: no
engine run and no provenance record; the dates it touches become stale.

preserve_on_empty (opt-in; the desktop Day page uses it): when a run would
place nothing at all on any generated date -- no work was allocated there,
or none of it fits -- while those dates still have saved placements, it
returns "nothing_placed" (with the run's genuine unscheduled reasons) and
writes nothing, so the previous schedule stays exactly as it was instead of
being replaced by an empty one. Without it (the default) such a run saves
the empty result, as before.

Freshness (day_freshness): per date, CURRENT / STALE (with a StaleReason) /
NONE, recomputed from the persisted record, the recorded range's inputs and
the saved placements. The record keeps only the *count* of unscheduled
tasks, so after a restart their explanations are unknown -- they are never
fabricated from the count.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from app.optimizer import MandatoryTaskSchedulingError
from app.planning.allocation import AllocationResult, allocate_tasks
from app.planning.application import (
    PROJECT_TEMPLATE,
    GenerationProvenance,
    PlacementReschedule,
    PlanningRange,
    PlanningService,
    RangeScope,
    RescheduleResult,
)
from app.planning.errors import (
    InvalidEntityError,
    PlanningError,
    RegenerationRequiredError,
    RescheduleRejectedError,
    ScopeError,
    StaleInputsError,
)
from app.planning.external_dependencies import (
    ExternalDependency,
    allocation_dates,
    explain_unallocated,
    satisfaction_instants,
)
from app.planning.models import DayScheduleOutput, FixedBlock, ScheduledTask, Task, TaskRegistry, compute_total_score
from app.planning.occurrence import HISTORY_PROTECTED_STATUSES
from app.planning.preferences import (
    DayPreferences,
    OptimizerMode,
    PreferenceOverrides,
    PreferenceRecord,
    resolve_day_preferences,
)
from app.planning.provenance import GenerationRecord, StaleReason, classify_generation, inputs_fingerprint, placements_digest
from app.planning.service import generate_selected_day
from app.planning.time import AmbiguousLocalTimeError, UnsupportedSchedulingWindowError

#: The largest date range one request may read, allocate or generate (a bounded, paginated-by-range API).
MAX_RANGE_DAYS = 62

#: Kept placements become engine-only reserved intervals under ids derived from theirs (never stored).
_RESERVED_NAMESPACE = uuid.UUID("5d2c7a3e-7a51-4e0e-9a55-2f0c3b1d4e6f")


class GenerationMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


class Freshness(str, Enum):
    #: Nothing has been generated or saved for the date.
    NONE = "none"
    #: The saved schedule matches the current inputs and was not changed since it was saved.
    CURRENT = "current"
    #: A saved schedule exists but is out of date (see stale_reason).
    STALE = "stale"


@dataclass(frozen=True)
class SchedulingInputs:
    """One consistent snapshot of everything a range generation reads, plus its fingerprint."""

    start_date: date_
    end_date: date_
    scope: RangeScope
    timezone_name: str
    planning_range: PlanningRange
    preferences_by_date: dict[date_, DayPreferences]
    external: dict[uuid.UUID, ExternalDependency]
    fingerprint: str


@dataclass(frozen=True)
class DayFreshness:
    date: date_
    status: Freshness
    stale_reason: StaleReason | None
    record: GenerationRecord | None
    placements: list[ScheduledTask]


@dataclass(frozen=True)
class AllocationPreview:
    """An allocation computed from persisted inputs (nothing saved), with the fingerprint that identifies them."""

    inputs: SchedulingInputs
    allocation: AllocationResult
    freshness: dict[date_, DayFreshness]

    @property
    def fingerprint(self) -> str:
        return self.inputs.fingerprint


@dataclass(frozen=True)
class DayPreferenceView:
    date: date_
    #: What scheduling uses on this date: defaults -> template -> user layer -> date layer.
    effective: DayPreferences
    #: What the date would use without its own layer (defaults -> template -> user layer).
    inherited: DayPreferences
    #: The date's own stored layer, if it has one.
    date_layer: PreferenceRecord | None


@dataclass(frozen=True)
class PreferenceViews:
    """Every layer of a range's preferences, as one consistent read."""

    timezone_name: str
    template: PreferenceOverrides | None
    user_layer: PreferenceRecord | None
    days: dict[date_, DayPreferenceView]


@dataclass(frozen=True)
class PlacementProblem:
    """Why a placement that would be kept no longer fits the current inputs."""

    placement_id: uuid.UUID
    task_id: uuid.UUID
    date: date_
    reason: str
    explanation: str


@dataclass(frozen=True)
class GenerationOutcome:
    #: "generated", "already_current" or (preserve_on_empty) "nothing_placed"; the latter two wrote nothing.
    status: str
    mode: GenerationMode
    inputs: SchedulingInputs
    allocation: AllocationResult
    #: Per generated date: the saved result (for already_current: the stored placements, no unscheduled reasons).
    outputs: dict[date_, DayScheduleOutput]
    #: Per generated date: the placements kept exactly as they were (incremental, or protected history).
    kept_ids: dict[date_, list[uuid.UUID]] = field(default_factory=dict)
    reschedule: RescheduleResult | None = None


def range_dates(start_date: date_, end_date: date_) -> list[date_]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def check_range(start_date: date_, end_date: date_) -> None:
    if end_date < start_date:
        raise ScopeError(f"end_date {end_date} is before start_date {start_date}.")
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise ScopeError(f"a range may span at most {MAX_RANGE_DAYS} days.")


# -----------------------------------------------------------------------------
# Inputs, allocation, freshness
# -----------------------------------------------------------------------------


def read_inputs(
    service: PlanningService,
    start_date: date_,
    end_date: date_,
    *,
    scope: RangeScope,
    timezone_name: str,
    template: object = PROJECT_TEMPLATE,
) -> SchedulingInputs:
    """Everything a generation of [start_date, end_date] reads, as one consistent snapshot."""
    dates = range_dates(start_date, end_date)
    with service.transaction():
        planning_range = service.load_range(start_date, end_date, scope=scope, timezone_name=timezone_name)
        preferences_by_date = service.resolve_preferences(dates, timezone_name, template=template)
        external = service.external_dependencies(planning_range.tasks.tasks.values(), start_date, end_date, timezone_name)
    fingerprint = inputs_fingerprint(
        start_date=start_date, end_date=end_date, scope=scope.value, timezone_name=timezone_name,
        tasks=planning_range.tasks.tasks.values(),
        fixed_blocks=[block for day in dates for block in planning_range.fixed_blocks_by_date[day]],
        preferences_by_date=preferences_by_date, external_dependencies=external,
    )
    return SchedulingInputs(
        start_date=start_date, end_date=end_date, scope=scope, timezone_name=timezone_name,
        planning_range=planning_range, preferences_by_date=preferences_by_date, external=external,
        fingerprint=fingerprint,
    )


def allocate(inputs: SchedulingInputs) -> AllocationResult:
    """Allocation of the inputs' range (never generates placements), with external-dependency explanations."""
    planning_range = inputs.planning_range
    result = allocate_tasks(
        start_date=inputs.start_date, end_date=inputs.end_date, tasks=planning_range.tasks,
        task_ids=planning_range.task_ids, preferences_by_date=inputs.preferences_by_date,
        fixed_blocks_by_date=planning_range.fixed_blocks_by_date,
        external_dependency_dates=allocation_dates(inputs.external),
    )
    return explain_unallocated(result, planning_range.tasks.tasks, inputs.external)


def preview_allocation(
    service: PlanningService,
    start_date: date_,
    end_date: date_,
    *,
    scope: RangeScope,
    timezone_name: str,
    template: object = PROJECT_TEMPLATE,
) -> AllocationPreview:
    """Allocate a range from persisted inputs (never generates or saves placements); see generate's expected_fingerprint."""
    check_range(start_date, end_date)
    inputs = read_inputs(service, start_date, end_date, scope=scope, timezone_name=timezone_name, template=template)
    allocation = allocate(inputs)
    freshness = day_freshness(service, range_dates(start_date, end_date), timezone_name, template=template)
    return AllocationPreview(inputs=inputs, allocation=allocation, freshness=freshness)


def preference_views(
    service: PlanningService,
    start_date: date_,
    end_date: date_,
    timezone_name: str,
    *,
    template: object = PROJECT_TEMPLATE,
) -> PreferenceViews:
    """Effective and inherited preferences of each date, with the stored layers they come from."""
    check_range(start_date, end_date)
    dates = range_dates(start_date, end_date)
    layer = service.preference_template() if template is PROJECT_TEMPLATE else template
    with service.transaction():
        user = service.user_preferences()
        by_date = service.date_preferences_for_range(start_date, end_date)
        effective = service.resolve_preferences(dates, timezone_name, template=layer)
    days = {
        day: DayPreferenceView(
            date=day, effective=effective[day],
            inherited=resolve_day_preferences(date=day, timezone=timezone_name, yaml_overrides=layer,
                                              user_overrides=user.overrides if user is not None else None),
            date_layer=by_date.get(day),
        )
        for day in dates
    }
    return PreferenceViews(timezone_name=timezone_name, template=layer, user_layer=user, days=days)


def day_freshness(
    service: PlanningService, dates: Iterable[date_], timezone_name: str, *, template: object = PROJECT_TEMPLATE
) -> dict[date_, DayFreshness]:
    """Each date's persisted freshness (see the module docstring); each recorded range's inputs are read once."""
    dates = sorted(set(dates))
    if not dates:
        return {}
    records = service.generation_records(dates[0], dates[-1])
    placements_by_date = service.placements_for_range(dates[0], dates[-1])
    fingerprints: dict[tuple, str | None] = {}
    result: dict[date_, DayFreshness] = {}
    for day in dates:
        record = records.get(day)
        placements = placements_by_date[day]
        current = None
        if record is not None:
            key = (record.range_start, record.range_end, record.range_scope)
            if key not in fingerprints:
                try:
                    fingerprints[key] = read_inputs(
                        service, record.range_start, record.range_end, scope=RangeScope(record.range_scope),
                        timezone_name=timezone_name, template=template,
                    ).fingerprint
                except (PlanningError, ValueError):
                    fingerprints[key] = None  # the recorded inputs can no longer be recomputed
            current = fingerprints[key]
        is_current, reason = classify_generation(
            record, has_placements=bool(placements), current_fingerprint=current,
            current_placements_digest=placements_digest(placements),
        )
        status = Freshness.NONE if is_current is None else Freshness.CURRENT if is_current else Freshness.STALE
        result[day] = DayFreshness(date=day, status=status, stale_reason=reason, record=record, placements=placements)
    return result


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------


def generate(
    service: PlanningService,
    *,
    range_start: date_,
    range_end: date_,
    generate_start: date_ | None = None,
    generate_end: date_ | None = None,
    scope: RangeScope = RangeScope.PLANNED,
    timezone_name: str,
    mode: GenerationMode = GenerationMode.FULL,
    protect_history: bool = False,
    expected_fingerprint: str | None = None,
    template: object = PROJECT_TEMPLATE,
    clock=None,
    preserve_on_empty: bool = False,
) -> GenerationOutcome:
    """
    Allocate [range_start, range_end] from persisted inputs and generate
    [generate_start, generate_end] (default: the whole range) -- see the
    module docstring. expected_fingerprint, when given, is the fingerprint
    of the allocation preview the caller acted on (StaleInputsError if the
    inputs changed since).
    """
    check_range(range_start, range_end)
    generate_start = generate_start or range_start
    generate_end = generate_end or range_end
    if not range_start <= generate_start <= generate_end <= range_end:
        raise ScopeError("the generated dates must lie inside the allocation range.")

    inputs = read_inputs(
        service, range_start, range_end, scope=scope, timezone_name=timezone_name, template=template
    )
    if expected_fingerprint is not None and expected_fingerprint != inputs.fingerprint:
        raise StaleInputsError(expected_fingerprint, inputs.fingerprint)
    allocation = allocate(inputs)
    dates = range_dates(generate_start, generate_end)

    freshness = day_freshness(service, dates, timezone_name, template=template)
    if all(state.status == Freshness.CURRENT for state in freshness.values()):
        outputs = {day: _stored_output(service, day, freshness[day], inputs) for day in dates}
        return GenerationOutcome(status="already_current", mode=mode, inputs=inputs, allocation=allocation, outputs=outputs)

    return generate_from(
        service, allocation, inputs, dates, mode=mode, protect_history=protect_history, template=template, clock=clock,
        preserve_on_empty=preserve_on_empty,
    )


def generate_from(
    service: PlanningService,
    allocation: AllocationResult,
    inputs: SchedulingInputs,
    dates: list[date_],
    *,
    mode: GenerationMode = GenerationMode.FULL,
    protect_history: bool = False,
    template: object = PROJECT_TEMPLATE,
    clock=None,
    preserve_on_empty: bool = False,
) -> GenerationOutcome:
    """Generate `dates` (contiguous, inside the inputs' range) from a given allocation and save them (see generate)."""
    outputs: dict[date_, DayScheduleOutput] = {}
    expected: dict[uuid.UUID, int] = {}
    kept: dict[date_, list[uuid.UUID]] = {}
    for day in dates:
        try:
            output, stored, kept_ids = _generate_date(
                service, allocation, inputs, day, mode=mode, protect_history=protect_history
            )
        except MandatoryTaskSchedulingError as error:
            error.failed_date = day  # which date could not be generated (the error names only tasks)
            raise
        outputs[day] = _with_category_snapshots(output, stored)
        expected.update({placement.id: placement.version for placement in stored})
        kept[day] = kept_ids

    if preserve_on_empty and expected and not any(output.placements for output in outputs.values()):
        # Nothing new to show and saved work would be wiped: keep the previous schedule untouched.
        return GenerationOutcome(status="nothing_placed", mode=mode, inputs=inputs, allocation=allocation,
                                 outputs=outputs, kept_ids=kept)

    now = (clock or _utcnow)()
    provenance = GenerationProvenance(
        allocation_id=allocation.id, range_start=inputs.start_date, range_end=inputs.end_date,
        range_scope=inputs.scope.value, fingerprint=inputs.fingerprint, timezone=inputs.timezone_name,
        engine_modes={day: inputs.preferences_by_date[day].optimizer_mode for day in dates}, generated_at=now,
    )
    with service.transaction():
        # The write transaction re-reads every input: a change since generation started -- to any task,
        # block, preference or external dependency, not just to placements -- refuses the save.
        again = read_inputs(
            service, inputs.start_date, inputs.end_date, scope=inputs.scope, timezone_name=inputs.timezone_name,
            template=template,
        )
        if again.fingerprint != inputs.fingerprint:
            raise StaleInputsError(inputs.fingerprint, again.fingerprint)
        result = service.reschedule_range(
            dates[0], dates[-1], outputs, expected_versions=expected, provenance=provenance
        )
    return GenerationOutcome(
        status="generated", mode=mode, inputs=inputs, allocation=allocation, outputs=outputs, kept_ids=kept,
        reschedule=result,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _with_category_snapshots(output: DayScheduleOutput, stored: list[ScheduledTask]) -> DayScheduleOutput:
    """
    The output as it will be saved: a placement the engine kept (same id)
    keeps its stored category snapshot, a new one takes its task's category
    now -- so what generation returns is exactly what is stored.
    """
    by_id = {placement.id: placement for placement in stored}
    placements = [
        placement.model_copy(update={"task_category": by_id[placement.id].task_category if placement.id in by_id
                                     else placement.task_category or output.tasks.get(placement.task_id).category})
        for placement in output.placements
    ]
    return output.model_copy(update={"placements": placements})


def _stored_output(service: PlanningService, day: date_, state: DayFreshness, inputs: SchedulingInputs) -> DayScheduleOutput:
    """The saved result of a date, without unscheduled explanations (only their count was saved)."""
    tz = state.record.timezone if state.record is not None else inputs.timezone_name
    return service.stored_day_output(day, tz) or DayScheduleOutput(date=day, timezone=tz)


def _generate_date(
    service: PlanningService,
    allocation: AllocationResult,
    inputs: SchedulingInputs,
    day: date_,
    *,
    mode: GenerationMode,
    protect_history: bool,
) -> tuple[DayScheduleOutput, list[ScheduledTask], list[uuid.UUID]]:
    """(the date's new output, its stored placements -- the save's precondition --, the kept placement ids)."""
    preferences = inputs.preferences_by_date[day]
    stored = service.placements_for_date(day)
    tasks = inputs.planning_range.tasks
    blocks = inputs.planning_range.fixed_blocks_by_date[day]

    if mode == GenerationMode.FULL and not protect_history:
        # The canonical full generation, exactly as the desktop has always run it.
        previous = service.stored_day_output(day, preferences.timezone)
        output, _ = generate_selected_day(
            allocation, day, tasks, {day: preferences}, {day: blocks}, previous_result=previous,
            external_dependency_satisfaction=satisfaction_instants(inputs.external),
        )
        return output, stored, []

    statuses = service.placement_execution_statuses(placement.id for placement in stored)
    if mode == GenerationMode.INCREMENTAL:
        kept = list(stored)
    else:
        kept = [placement for placement in stored if statuses.get(placement.id) in HISTORY_PROTECTED_STATUSES]
    kept_tasks = service.get_tasks_including_deleted(placement.task_id for placement in kept)

    problems = _kept_problems(service, kept, kept_tasks, inputs, day, full_check=mode == GenerationMode.INCREMENTAL)
    if problems:
        raise RegenerationRequiredError(problems)

    # Work that must not be placed (again) on this date: kept work itself, and -- so an occurrence is never
    # duplicated -- tasks whose occurrence already has a placement elsewhere that this run will not replace.
    candidates = {task_id for task_id, assigned in allocation.assignments.items() if assigned == day}
    elsewhere = _placements_elsewhere(service, candidates, tasks, day, protected_only=mode == GenerationMode.FULL)
    exclude = {placement.task_id for placement in kept} | set(elsewhere)
    satisfaction = dict(satisfaction_instants(inputs.external))
    for placement in [*kept, *elsewhere.values()]:
        satisfaction[placement.task_id] = (placement.planned_date, placement.planned_end)

    reserved = [
        FixedBlock(
            id=uuid.uuid5(_RESERVED_NAMESPACE, str(placement.id)), user_id=placement.user_id, label="(kept work)",
            category="reserved", planned_date=day, timezone=preferences.timezone,
            planned_start=placement.planned_start, planned_end=placement.planned_end,
        )
        for placement in kept
    ]
    sub_allocation = allocation.model_copy(update={
        "assignments": {task_id: assigned for task_id, assigned in allocation.assignments.items() if task_id not in exclude}
    })
    kept_ids = {placement.id for placement in kept}
    previous = None
    if mode == GenerationMode.FULL:
        replaceable = [placement for placement in stored if placement.id not in kept_ids]
        previous = DayScheduleOutput(
            date=day, timezone=preferences.timezone,
            tasks=service.get_tasks(placement.task_id for placement in replaceable),
            placements=replaceable, total_score=compute_total_score(replaceable),
        ) if replaceable else None
    generated, _ = generate_selected_day(
        sub_allocation, day, tasks, {day: preferences}, {day: [*blocks, *reserved]}, previous_result=previous,
        external_dependency_satisfaction=satisfaction,
    )

    registry = TaskRegistry(tasks={**generated.tasks.tasks, **{p.task_id: kept_tasks[p.task_id] for p in kept}})
    placements = [*kept, *generated.placements]
    output = DayScheduleOutput(
        date=day, timezone=generated.timezone, fixed_blocks=list(blocks), tasks=registry, placements=placements,
        unscheduled=generated.unscheduled, total_score=compute_total_score(placements),
    )
    return output, stored, sorted(kept_ids, key=str)


def _placements_elsewhere(
    service: PlanningService, task_ids: set[uuid.UUID], tasks: TaskRegistry, day: date_, *, protected_only: bool
) -> dict[uuid.UUID, ScheduledTask]:
    """For each task (in task_ids) whose occurrence for `day` already has a live placement on another date: the earliest."""
    found: dict[uuid.UUID, ScheduledTask] = {}
    groups = service.active_placements_for_tasks(task_ids)
    statuses = service.placement_execution_statuses(
        placement.id for group in groups.values() for placement in group
    ) if protected_only else {}
    for task_id, group in groups.items():
        task = tasks.get(task_id)
        if task is None:
            continue
        for placement in group:
            if placement.planned_date == day:
                continue
            if task.recurrence is not None:
                continue  # each date of a recurring template is its own occurrence
            if protected_only and statuses.get(placement.id) not in HISTORY_PROTECTED_STATUSES:
                continue
            found.setdefault(task_id, placement)
    return found


def _minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60


def _kept_problems(
    service: PlanningService,
    kept: list[ScheduledTask],
    kept_tasks: Mapping[uuid.UUID, Task],
    inputs: SchedulingInputs,
    day: date_,
    *,
    full_check: bool,
) -> list[PlacementProblem]:
    """
    Why kept placements cannot be kept. Every kept placement must still be
    reservable (inside the date's window, clear of fixed blocks and of each
    other); with full_check (incremental) it must also still be a valid
    placement of its task under the current inputs.
    """
    preferences = inputs.preferences_by_date[day]
    blocks = inputs.planning_range.fixed_blocks_by_date[day]
    problems: list[PlacementProblem] = []

    def problem(placement: ScheduledTask, reason: str, explanation: str) -> None:
        problems.append(PlacementProblem(placement.id, placement.task_id, day, reason, explanation))

    try:
        window_start, window_end = preferences.to_local_day_window().to_utc_instants()
    except (UnsupportedSchedulingWindowError, AmbiguousLocalTimeError) as error:
        for placement in kept:
            problem(placement, "unsupported_day_window", str(error))
        return problems

    in_range = inputs.planning_range.tasks
    dependency_ids = {dep for task in kept_tasks.values() for dep in task.dependency_ids}
    dependency_placements = service.active_placements_for_tasks(dependency_ids) if full_check else {}
    kept_by_task = {placement.task_id: placement for placement in kept}

    for placement in sorted(kept, key=lambda item: (item.planned_start, str(item.id))):
        task = kept_tasks.get(placement.task_id)
        if placement.planned_start < window_start or placement.planned_end > window_end:
            problem(placement, "outside_day_window", "it lies outside this date's day window.")
        clash = next(
            (b for b in blocks if placement.planned_start < b.planned_end and b.planned_start < placement.planned_end), None
        )
        if clash is not None:
            problem(placement, "overlaps_fixed_block", f"it overlaps the fixed block {clash.label!r}.")
        other = next((o for o in kept if o.id != placement.id
                      and placement.planned_start < o.planned_end and o.planned_start < placement.planned_end), None)
        if other is not None:
            problem(placement, "overlaps_placement", f"it overlaps the saved placement {other.id}.")
        if not full_check:
            continue
        if task is None or task.deleted_at is not None:
            problem(placement, "task_deleted", "its task was deleted.")
            continue
        if task.id not in in_range:
            problem(placement, "task_out_of_range", "its task is no longer planned in this range.")
            continue
        if task.required_date is not None and task.required_date != day:
            problem(placement, "required_date_changed", f"its task must now happen on {task.required_date}.")
        if round(_minutes(placement.planned_start, placement.planned_end)) != task.estimated_duration_minutes:
            problem(placement, "duration_changed", f"its task now takes {task.estimated_duration_minutes} minutes.")
        if task.deadline is not None and placement.planned_end > task.deadline:
            problem(placement, "deadline_missed", f"it would end after the task's deadline ({task.deadline.isoformat()}).")
        if (
            preferences.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
            and task.estimated_duration_minutes > 30
            and _local_minute(placement.planned_start, preferences.timezone) % 15 != 0
        ):
            problem(placement, "engine_mode_changed",
                    "the adhd_friendly engine starts tasks over 30 minutes on quarter hours only.")
        for dependency in task.dependency_ids:
            if not _dependency_done_by(dependency, placement, day, kept_by_task, dependency_placements, inputs.external):
                problem(placement, "dependency_not_satisfied", f"its dependency {dependency} no longer finishes before it.")
                break
    return problems


def _local_minute(instant: datetime, tz_name: str) -> int:
    local = instant.astimezone(ZoneInfo(tz_name))
    return local.hour * 60 + local.minute


def _dependency_done_by(
    dependency: uuid.UUID,
    placement: ScheduledTask,
    day: date_,
    kept_by_task: Mapping[uuid.UUID, ScheduledTask],
    dependency_placements: Mapping[uuid.UUID, list[ScheduledTask]],
    external: Mapping[uuid.UUID, ExternalDependency],
) -> bool:
    same_day = kept_by_task.get(dependency)
    if same_day is not None:
        return same_day.planned_end <= placement.planned_start
    if any(other.planned_date < day for other in dependency_placements.get(dependency, [])):
        return True
    resolved = external.get(dependency)
    if resolved is not None and resolved.satisfied:
        return resolved.satisfied_date < day or resolved.satisfied_at <= placement.planned_start
    return False


# -----------------------------------------------------------------------------
# Explicit rescheduling (docs/execution-rescheduling.md)
# -----------------------------------------------------------------------------


def reschedule_placement(
    service: PlanningService,
    placement_id: uuid.UUID,
    *,
    expected_version: int,
    planned_date: date_,
    timezone_name: str,
    planned_start: datetime,
    planned_end: datetime,
    replacement_id: uuid.UUID | None = None,
    task_category: str | None = None,
    at: datetime | None = None,
    template: object = PROJECT_TEMPLATE,
) -> PlacementReschedule:
    """
    Move one saved placement to a new interval -- explicitly, atomically and
    without rewriting history -- in one transaction:

    1. the source must be a live placement at `expected_version`, of a live
       task, whose execution (if any) has not started
       (PlanningService.reschedule_source: EntityNotFoundError,
       VersionConflictError, InvalidReferenceError, HistoryProtectedError);
    2. the destination must satisfy the hard scheduling rules of the day
       engine and of incremental generation (reschedule_problems), judged
       against what is stored inside this transaction -- else
       RescheduleRejectedError listing every problem;
    3. PlanningService.apply_reschedule tombstones the source (reason
       RESCHEDULED, superseded_by_id = the replacement), inserts the
       replacement (replacement_id, or a new id) and cancels the source's
       never-started execution at `at`.

    A rejected move changes nothing. The saved schedule of the affected
    dates becomes stale (its placements changed); nothing is regenerated.
    """
    with service.transaction():
        previous, task = service.reschedule_source(placement_id, expected_version=expected_version)
        try:
            candidate = ScheduledTask(
                id=replacement_id or uuid.uuid4(), task_id=task.id, user_id=previous.user_id,
                planned_date=planned_date, timezone=timezone_name, planned_start=planned_start,
                planned_end=planned_end, task_category=task_category,
            )
        except ValueError as error:
            raise InvalidEntityError(f"the destination is not a valid placement: {error}") from None
        problems = reschedule_problems(service, previous, candidate, task, template=template)
        if problems:
            raise RescheduleRejectedError(problems)
        return service.apply_reschedule(placement_id, expected_version=expected_version, replacement=candidate, at=at)


def reschedule_problems(
    service: PlanningService,
    previous: ScheduledTask,
    candidate: ScheduledTask,
    task: Task,
    *,
    template: object = PROJECT_TEMPLATE,
) -> list[PlacementProblem]:
    """
    Why `candidate` (the destination of moving `previous`) breaks a hard rule;
    empty when the move is valid. The destination must: start on its
    planned_date in its timezone, on whole minutes; differ from where the
    placement already is; keep a recurring template's occurrence on its own
    date (each date of a template is its own occurrence -- recurrence is not
    expanded); honour the task's required date and deadline; and, like a
    placement kept by incremental generation (_kept_problems), lie inside the
    date's day window, clear of fixed blocks and of the date's other
    placements, last exactly the task's estimate, fit the engine mode and
    start after its dependencies finish. Tasks that depend on it must still
    start after it ends.
    """
    day = candidate.planned_date
    problems: list[PlacementProblem] = []

    def problem(reason: str, explanation: str) -> None:
        problems.append(PlacementProblem(candidate.id, task.id, day, reason, explanation))

    local_start = candidate.planned_start.astimezone(ZoneInfo(candidate.timezone))
    if local_start.date() != day:
        problem("wrong_date", f"it would start on {local_start.date()} in {candidate.timezone}, not on {day}.")
    if any(instant.second or instant.microsecond for instant in (candidate.planned_start, candidate.planned_end)):
        problem("not_whole_minutes", "it must start and end on whole minutes.")
    if (candidate.planned_date, candidate.timezone, candidate.planned_start, candidate.planned_end) == (
            previous.planned_date, previous.timezone, previous.planned_start, previous.planned_end):
        problem("unchanged", "it is already planned there.")
    if task.recurrence is not None and day != previous.planned_date:
        problem("recurring_occurrence_date",
                "a recurring task's placement is the occurrence of its own date; it can only move within that date.")
    if task.required_date is not None and task.required_date != day:
        problem("required_date", f"its task must happen on {task.required_date}.")
    if task.deadline is not None and candidate.planned_end > task.deadline:
        problem("deadline_missed", f"it would end after the task's deadline ({task.deadline.isoformat()}).")
    if problems:
        return problems

    inputs = read_inputs(service, day, day, scope=RangeScope.ELIGIBLE, timezone_name=candidate.timezone,
                         template=template)
    others = [placement for placement in service.placements_for_date(day) if placement.id != previous.id]
    kept_tasks = service.get_tasks_including_deleted({task.id, *(placement.task_id for placement in others)})
    problems.extend(
        found for found in _kept_problems(service, [candidate, *others], kept_tasks, inputs, day, full_check=True)
        if found.placement_id == candidate.id
    )
    if task.recurrence is not None and any(placement.task_id == task.id for placement in others):
        problem("occurrence_taken", f"its recurring task already has a placement on {day}.")

    dependents = [other.id for other in service.list_tasks() if task.id in other.dependency_ids]
    grouped = service.active_placements_for_tasks(dependents)
    for dependent_id in sorted(grouped, key=str):
        if any(placement.planned_date < day
               or (placement.planned_date == day and placement.planned_start < candidate.planned_end)
               for placement in grouped[dependent_id]):
            problem("dependent_starts_first",
                    f"the task {dependent_id} depends on it and is planned to start before it would end.")
    return problems
