"""
app/planning/series.py

Recurring series as concrete work (docs/recurrence.md): materializing the
occurrences of a date range, and editing or deleting "this occurrence",
"this and every later occurrence" or "the entire series". Works on any
PlanningService (the SQLite repository, or the server's), through its public
methods only, inside one service transaction per operation -- so the
desktop, the local web profile and the server share one implementation.

Expansion (expand_occurrences), for an explicit inclusive date range bounded
by the planning-range limit (MAX_EXPANSION_DAYS) and a materialized-row
budget (MAX_EXPANSION_OCCURRENCES):

    - every live, configured series contributes its slots in the range
      (app/planning/recurrence.py; arithmetic seeking, bounded scans). Each
      slot's occurrence id is derived from (series id, slot), so expanding
      the same or an overlapping range again, after a restart, after a
      failed attempt, or on another device, finds the existing record --
      live or tombstoned -- and does nothing: a skipped, deleted or
      superseded slot is never minted again;
    - a slot that a preserved occurrence of an earlier segment of the same
      lineage still covers (a "this and every later occurrence" change kept
      completed or individually edited work) is not minted for the new
      segment, so an edit never duplicates a slot's work;
    - a series' dependency on another series resolves, per slot, to the
      prerequisite lineage's occurrence of the *same* slot (materialized
      too when needed, within the same budget). A prerequisite without such
      a slot, or with an incompatible cadence or time zone, is an
      unresolved-dependency problem and that slot is not materialized;
    - a series without an explicit start date and time zone needs
      configuration and produces no slots. Its legacy placements (saved
      before expansion) are mapped: a date with exactly one live legacy
      placement gets the occurrence of that slot, whose placement then
      supersedes the legacy one by occurrence identity
      (app/planning/occurrence.py); a date with several live legacy
      placements is an ambiguous collision, reported for repair and left
      untouched -- nothing is discarded;
    - a slot on a date whose clocks change in the series' time zone is
      reported (a window crossing the change cannot be scheduled that day);
      no time is ever shifted silently.

Nothing is written unless the whole expansion fits the budget and every
created record validates; then all of it is written in one transaction.

Scoped changes (edit_occurrence, edit_series, delete_occurrence,
delete_series) are atomic and versioned: the target record's expected
version is the precondition, every other record touched is read and written
inside the same transaction. Policies:

    this occurrence      edit_occurrence: the occurrence's own content (and
                         date: moving it keeps its slot identity); it becomes
                         MODIFIED, so series-wide edits leave it alone.
                         delete_occurrence: a tombstone (SKIPPED or DELETED)
                         that keeps its slot reserved.
    this and later       a split at the occurrence's original slot (the
    (FUTURE)             cutoff): the series segment ends the day before the
                         cutoff (count becomes the equivalent end date) and,
                         for an edit, a new segment with the new definition
                         starts at the cutoff, linked by series_predecessor_id
                         (explicit lineage, never overlapping: the old segment
                         has no slot from the cutoff on). Its occurrences from
                         the cutoff on are superseded (tombstones, SUPERSEDED),
                         except preserved ones.
    entire series        an edit that keeps the rule updates the definition in
    (SERIES)             place and refreshes every occurrence that still
                         follows it; one that changes the rule (cadence,
                         bounds, start date or time zone -- a time zone edit is
                         a versioned series change) is a split at the
                         segment's start, so historical occurrences keep their
                         ids and slots. delete_series tombstones the definition
                         and supersedes its occurrences, except preserved ones.

Preserved, never superseded or rewritten: occurrences whose execution has
started or finished (in progress, paused, completed, skipped, cancelled --
HISTORY_PROTECTED_STATUSES) and individually edited (MODIFIED) occurrences.
Every result names them with the reason. Execution records are never
touched, and nothing is ever hard-deleted. "Entire series" is the segment
the target belongs to; earlier segments split off by a "this and later"
change keep their own history.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.planning.application import PlanningService
from app.planning.errors import (
    EntityNotFoundError,
    RecurrenceLimitError,
    ScopeError,
    SeriesConfigurationError,
    VersionConflictError,
)
from app.planning.models import OccurrenceState, ScheduledTask, Task
from app.planning.occurrence import HISTORY_PROTECTED_STATUSES
from app.planning.recurrence import (
    RecurrenceBudgetError,
    SeriesRule,
    cadence_problem,
    occurrence_task_id,
    offset_transition,
)
from app.planning.time import AmbiguousLocalTimeError, local_instant

#: The widest range one expansion may cover: the planning-range limit (app.planning.workflow.MAX_RANGE_DAYS).
MAX_EXPANSION_DAYS = 62
#: The most occurrences one expansion may materialize (prerequisites included).
MAX_EXPANSION_OCCURRENCES = 500
#: The most existing occurrences one scoped change may refresh, supersede or preserve.
MAX_CHANGE_OCCURRENCES = 5000
#: The most segments a lineage walk follows.
MAX_LINEAGE = 64

#: The content fields an occurrence takes from its series definition.
_CONTENT_FIELDS = (
    "project_id", "name", "category", "tags", "estimated_duration_minutes", "priority", "points", "required",
    "preferred_time", "preferred_time_window",
)
#: The occurrence fields its series dictates (public: synchronization re-derives an occurrence from a series).
SERIES_CONTENT_FIELDS = _CONTENT_FIELDS


class EditScope(str, Enum):
    OCCURRENCE = "occurrence"
    FUTURE = "future"
    SERIES = "series"


@dataclass(frozen=True)
class SlotProblem:
    series_id: uuid.UUID
    slot: date_ | None
    code: str
    message: str


@dataclass(frozen=True)
class LegacyCollision:
    """Several live legacy placements of one series on one date: which is the slot's work is ambiguous."""

    series_id: uuid.UUID
    slot: date_
    placement_ids: list[uuid.UUID]


@dataclass(frozen=True)
class ExpansionResult:
    start_date: date_
    end_date: date_
    #: Occurrences materialized by this call (prerequisites included); empty for a repeated expansion.
    created: list[Task]
    #: Slots that already had a record (live or tombstoned) or are covered by a preserved earlier segment.
    existing_count: int
    #: Series without an explicit start date and time zone (they produce no slots).
    needs_configuration: list[uuid.UUID] = field(default_factory=list)
    #: Slots left unmaterialized, with the reason (e.g. dependency_unresolved).
    problems: list[SlotProblem] = field(default_factory=list)
    #: Materialized, but worth knowing (daylight-saving changes, unmapped legacy placements).
    warnings: list[SlotProblem] = field(default_factory=list)
    legacy_collisions: list[LegacyCollision] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True)
class PreservedOccurrence:
    task: Task
    #: "history" (its execution started or finished) or "modified" (edited on its own).
    reason: str


@dataclass(frozen=True)
class SeriesChange:
    scope: EditScope
    #: The series definition as stored after the change (ended, updated or deleted), if one was involved.
    series: Task | None = None
    #: A new segment continuing the series (a split).
    successor: Task | None = None
    #: The single occurrence edited or deleted (scope OCCURRENCE).
    occurrence: Task | None = None
    #: Occurrences refreshed to the new definition.
    updated: list[Task] = field(default_factory=list)
    #: Occurrences tombstoned as SUPERSEDED (their slots stay reserved).
    superseded: list[Task] = field(default_factory=list)
    preserved: list[PreservedOccurrence] = field(default_factory=list)
    problems: list[SlotProblem] = field(default_factory=list)

    def explanation(self) -> str:
        """What was preserved and why, in words (for a confirmation or a result message)."""
        if not self.preserved:
            return ""
        history = [item.task for item in self.preserved if item.reason == "history"]
        modified = [item.task for item in self.preserved if item.reason == "modified"]
        parts = []
        if history:
            parts.append(f"{len(history)} occurrence(s) already started or finished were kept as history")
        if modified:
            parts.append(f"{len(modified)} individually edited occurrence(s) were kept")
        return "; ".join(parts) + "."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def occurrence_for(series: Task, slot: date_, dependency_ids: list[uuid.UUID], now: datetime) -> Task:
    """The occurrence of `series` at `slot`, as the series definition describes it now."""
    return Task(
        id=occurrence_task_id(series.id, slot), user_id=series.user_id,
        **{name: getattr(series, name) for name in _CONTENT_FIELDS},
        required_date=slot, dependency_ids=list(dependency_ids),
        series_id=series.id, occurrence_slot=slot, series_version=series.version,
        created_at=now, updated_at=now,
    )


# -----------------------------------------------------------------------------
# Expansion
# -----------------------------------------------------------------------------


class _Lineage:
    """Cached reads of series, occurrences and lineage links within one operation (one transaction)."""

    def __init__(self, service: PlanningService) -> None:
        self.service = service
        self._tasks: dict[uuid.UUID, Task | None] = {}
        self._successors: dict[uuid.UUID, list[Task]] = {}

    def tasks(self, ids) -> dict[uuid.UUID, Task]:
        wanted = [task_id for task_id in dict.fromkeys(ids) if task_id not in self._tasks]
        if wanted:
            found = self.service.get_tasks_including_deleted(wanted)
            for task_id in wanted:
                self._tasks[task_id] = found.get(task_id)
        return {task_id: self._tasks[task_id] for task_id in ids if self._tasks.get(task_id) is not None}

    def task(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks([task_id]).get(task_id)

    def remember(self, task: Task) -> None:
        self._tasks[task.id] = task

    def segments(self, series: Task) -> list[Task]:
        """Every segment of the series' lineage (predecessors, itself, successors), bounded."""
        chain: list[Task] = []
        current, steps = series, 0
        while current is not None and current.series_predecessor_id is not None and steps < MAX_LINEAGE:
            current = self.task(current.series_predecessor_id)
            if current is not None:
                chain.insert(0, current)
            steps += 1
        chain.append(series)
        frontier, seen = [series], {task.id for task in chain}
        while frontier and len(chain) < MAX_LINEAGE:
            ids = [task.id for task in frontier if task.id not in self._successors]
            if ids:
                found = self.service.series_successors(ids)
                for task_id in ids:
                    self._successors[task_id] = found.get(task_id, [])
            frontier = [nxt for task in frontier for nxt in self._successors[task.id] if nxt.id not in seen]
            for task in frontier:
                seen.add(task.id)
                chain.append(task)
                self.remember(task)
        return chain

    def predecessors(self, series: Task) -> list[Task]:
        chain = self.segments(series)
        return chain[:chain.index(series)] if series in chain else []

    def covering_predecessor_occurrence(self, series: Task, slot: date_) -> Task | None:
        """A preserved occurrence of an earlier segment that still does `slot`'s work (see the module docstring)."""
        earlier = self.predecessors(series)
        if not earlier:
            return None
        found = self.tasks([occurrence_task_id(segment.id, slot) for segment in earlier])
        for occurrence in found.values():
            if occurrence.deleted_at is None or occurrence.occurrence_state != OccurrenceState.SUPERSEDED:
                return occurrence
        return None


class _Expander:
    def __init__(self, service: PlanningService, *, budget: int, now: datetime) -> None:
        self.service = service
        self.lineage = _Lineage(service)
        self.budget = budget
        self.now = now
        self.created: dict[uuid.UUID, Task] = {}
        self.existing = 0
        self.problems: list[SlotProblem] = []
        self.warnings: list[SlotProblem] = []
        self.collisions: list[LegacyCollision] = []
        self.needs_configuration: list[uuid.UUID] = []

    def exists(self, occurrence_id: uuid.UUID) -> bool:
        return occurrence_id in self.created or self.lineage.task(occurrence_id) is not None

    # -- one series ------------------------------------------------------------------

    def expand(self, series: Task, first: date_, last: date_, legacy: dict[date_, list[ScheduledTask]]) -> None:
        if series.needs_configuration:
            self.needs_configuration.append(series.id)
            for day in sorted(legacy):
                self._legacy_slot(series, day, legacy[day])
            return
        rule = SeriesRule.of(series.recurrence)
        try:
            slots = list(rule.slots(first, last))
        except RecurrenceBudgetError as error:
            raise RecurrenceLimitError(str(error)) from None
        for slot in slots:
            if len(legacy.get(slot, [])) > 1:
                self._collision(series, slot, legacy[slot])
                continue
            self.slot(series, slot)
        for day in sorted(set(legacy) - set(slots)):
            self.warnings.append(SlotProblem(
                series.id, day, "legacy_placement_unmapped",
                f"{series.name!r} has a placement saved on {day} before recurrence was expanded, but its rule has no "
                "occurrence that day; it is not an occurrence, and regenerating that date removes it.",
            ))

    def _legacy_slot(self, series: Task, day: date_, placements: list[ScheduledTask]) -> None:
        if len(placements) > 1:
            self._collision(series, day, placements)
            return
        self.slot(series, day)

    def _collision(self, series: Task, day: date_, placements: list[ScheduledTask]) -> None:
        self.collisions.append(LegacyCollision(series.id, day, sorted((p.id for p in placements), key=str)))
        self.problems.append(SlotProblem(
            series.id, day, "legacy_collision",
            f"{series.name!r} has {len(placements)} placements on {day} saved before recurrence was expanded; which "
            "one is that day's occurrence is ambiguous. Remove the extra placement(s), then expand again.",
        ))

    def slot(self, series: Task, slot: date_) -> uuid.UUID | None:
        """The occurrence doing `slot`'s work for this segment: existing, covered, or materialized now."""
        own = occurrence_task_id(series.id, slot)
        if self.exists(own):
            self.existing += 1
            return own
        covered = self.lineage.covering_predecessor_occurrence(series, slot)
        if covered is not None:
            self.existing += 1
            return covered.id
        return self._materialize(series, slot, depth=0)

    def _materialize(self, series: Task, slot: date_, *, depth: int) -> uuid.UUID | None:
        dependencies: list[uuid.UUID] = []
        for dependency_id in series.dependency_ids:
            target = self.lineage.task(dependency_id)
            if target is None or not target.is_series:
                dependencies.append(dependency_id)
                continue
            prerequisite, problem = self._prerequisite(series, target, slot, depth)
            if problem is not None:
                self.problems.append(SlotProblem(series.id, slot, "dependency_unresolved", problem))
                return None
            dependencies.append(prerequisite)
        if len(self.created) >= self.budget:
            raise RecurrenceLimitError(
                f"materializing these occurrences would create more than {self.budget}; expand a smaller range."
            )
        occurrence = occurrence_for(series, slot, dependencies, self.now)
        self.created[occurrence.id] = occurrence
        self._warn_about_clocks(series, occurrence)
        return occurrence.id

    def _prerequisite(self, dependent: Task, prerequisite: Task, slot: date_, depth: int) -> tuple[uuid.UUID | None, str | None]:
        if depth >= MAX_LINEAGE:
            return None, "the chain of recurring prerequisites is too deep."
        if dependent.needs_configuration:
            return None, (f"it depends on the recurring series {prerequisite.name!r}; configure this series' start "
                          "date and time zone so each occurrence can find the prerequisite's occurrence of its date.")
        for segment in self.lineage.segments(prerequisite):
            if segment.deleted_at is not None or segment.needs_configuration or not segment.is_series:
                continue
            rule = SeriesRule.of(segment.recurrence)
            if not rule.is_slot(slot):
                continue
            problem = cadence_problem(SeriesRule.of(dependent.recurrence), rule)
            if problem is not None:
                return None, f"its prerequisite {segment.name!r}: {problem}"
            own = occurrence_task_id(segment.id, slot)
            if self.exists(own):
                return own, None
            covered = self.lineage.covering_predecessor_occurrence(segment, slot)
            if covered is not None:
                return covered.id, None
            created = self._materialize(segment, slot, depth=depth + 1)
            if created is None:
                return None, f"its prerequisite {segment.name!r} cannot be materialized on {slot}."
            return created, None
        return None, (f"its prerequisite series {prerequisite.name!r} has no occurrence on {slot} (it starts later, "
                      "ended, was deleted, or repeats on other dates).")

    def _warn_about_clocks(self, series: Task, occurrence: Task) -> None:
        tz_name = series.recurrence.timezone if series.recurrence is not None else None
        if tz_name is None:
            return
        slot = occurrence.occurrence_slot
        if offset_transition(slot, tz_name):
            self.warnings.append(SlotProblem(
                series.id, slot, "offset_transition",
                f"The clocks change on {slot} in {tz_name}: a time window crossing the change cannot be scheduled "
                "that day (it is refused explicitly, never shifted).",
            ))
        window = occurrence.preferred_time_window
        if window is None:
            return
        for minute in (window.start_minute, window.end_minute):
            if minute >= 1440:
                continue
            try:
                local_instant(slot, minute, tz_name)
            except AmbiguousLocalTimeError as error:
                self.warnings.append(SlotProblem(series.id, slot, "ambiguous_local_time", str(error)))


def expand_occurrences(
    service: PlanningService,
    start_date: date_,
    end_date: date_,
    *,
    series_ids: list[uuid.UUID] | None = None,
    budget: int = MAX_EXPANSION_OCCURRENCES,
    dry_run: bool = False,
    clock=None,
) -> ExpansionResult:
    """Materialize the occurrences of [start_date, end_date] (see the module docstring); dry_run writes nothing."""
    if end_date < start_date:
        raise ScopeError(f"end_date {end_date} is before start_date {start_date}.")
    if (end_date - start_date).days + 1 > MAX_EXPANSION_DAYS:
        raise ScopeError(f"a range may span at most {MAX_EXPANSION_DAYS} days.")
    with service.transaction():
        expander = _Expander(service, budget=budget, now=(clock or _utcnow)())
        series = service.list_series()
        if series_ids is not None:
            wanted = set(series_ids)
            series = [task for task in series if task.id in wanted]
        legacy: dict[uuid.UUID, dict[date_, list[ScheduledTask]]] = defaultdict(lambda: defaultdict(list))
        for task_id, placements in service.active_placements_for_tasks(task.id for task in series).items():
            for placement in placements:
                if start_date <= placement.planned_date <= end_date:
                    legacy[task_id][placement.planned_date].append(placement)
        for task in series:
            expander.expand(task, start_date, end_date, legacy.get(task.id, {}))
        created = list(expander.created.values())
        if created and not dry_run:
            created = service.materialize_occurrences(created)
    return ExpansionResult(
        start_date=start_date, end_date=end_date, created=created, existing_count=expander.existing,
        needs_configuration=expander.needs_configuration, problems=expander.problems, warnings=expander.warnings,
        legacy_collisions=expander.collisions, dry_run=dry_run,
    )


# -----------------------------------------------------------------------------
# Scoped changes
# -----------------------------------------------------------------------------


def _require(service: PlanningService, task_id: uuid.UUID, expected_version: int, label: str) -> Task:
    stored = service.get_tasks_including_deleted([task_id]).get(task_id)
    if stored is None:
        raise EntityNotFoundError(label, task_id)
    if stored.deleted_at is not None or stored.version != expected_version:
        raise VersionConflictError(label, task_id, expected_version=expected_version, current_version=stored.version,
                                   deleted=stored.deleted_at is not None)
    return stored


def _split_existing(
    service: PlanningService, series: Task, cutoff: date_
) -> tuple[list[Task], list[PreservedOccurrence]]:
    """The series' live occurrences from `cutoff` on: (those to supersede, those preserved and why)."""
    occurrences = [
        task for task in service.occurrences_of_series([series.id], include_deleted=False).get(series.id, [])
        if task.occurrence_slot >= cutoff
    ]
    if len(occurrences) > MAX_CHANGE_OCCURRENCES:
        raise RecurrenceLimitError(f"this change would touch more than {MAX_CHANGE_OCCURRENCES} occurrences.")
    statuses = service.execution_statuses_for_tasks(task.id for task in occurrences)
    supersede: list[Task] = []
    preserved: list[PreservedOccurrence] = []
    for task in occurrences:
        if statuses.get(task.id, set()) & HISTORY_PROTECTED_STATUSES:
            preserved.append(PreservedOccurrence(task, "history"))
        elif task.occurrence_state == OccurrenceState.MODIFIED:
            preserved.append(PreservedOccurrence(task, "modified"))
        else:
            supersede.append(task)
    return supersede, preserved


def _supersede(service: PlanningService, occurrences: list[Task]) -> list[Task]:
    if not occurrences:
        return []
    service.delete_occurrences({task.id: task.version for task in occurrences}, state=OccurrenceState.SUPERSEDED)
    stored = service.get_tasks_including_deleted(task.id for task in occurrences)
    return [stored[task.id] for task in occurrences]


def _ended(series: Task, cutoff: date_) -> Task:
    """The segment ending the day before `cutoff` (a retired segment when that precedes its start)."""
    end = cutoff - timedelta(days=1)
    return series.model_copy(update={"recurrence": series.recurrence.model_copy(update={"end_date": end, "count": None})})


def _same_rule(left: Task, right: Task) -> bool:
    return left.recurrence == right.recurrence


def edit_occurrence(service: PlanningService, edited: Task, *, expected_version: int) -> SeriesChange:
    """
    "This occurrence": save the occurrence's own content (a new required
    date moves it; its slot identity stays). It becomes MODIFIED.
    """
    with service.transaction():
        stored = _require(service, edited.id, expected_version, "task")
        if not stored.is_occurrence:
            raise SeriesConfigurationError("only an occurrence of a recurring series can be edited on its own here.")
        state = stored.occurrence_state or OccurrenceState.MODIFIED
        saved = service.update_task(edited.model_copy(update={"occurrence_state": state}),
                                    expected_version=expected_version)
    return SeriesChange(EditScope.OCCURRENCE, occurrence=saved)


def delete_occurrence(service: PlanningService, occurrence_id: uuid.UUID, *, expected_version: int,
                      skip: bool = False) -> SeriesChange:
    """"This occurrence": a tombstone (SKIPPED or DELETED) that keeps its slot reserved; its placements go with it."""
    with service.transaction():
        stored = _require(service, occurrence_id, expected_version, "task")
        if not stored.is_occurrence:
            raise SeriesConfigurationError("only an occurrence of a recurring series can be skipped or deleted on its own.")
        service.delete_occurrences({occurrence_id: expected_version},
                                   state=OccurrenceState.SKIPPED if skip else OccurrenceState.DELETED)
        removed = service.get_tasks_including_deleted([occurrence_id])[occurrence_id]
    return SeriesChange(EditScope.OCCURRENCE, occurrence=removed)


def edit_series(
    service: PlanningService,
    definition: Task,
    *,
    expected_version: int,
    scope: EditScope,
    cutoff: date_ | None = None,
    clock=None,
) -> SeriesChange:
    """
    Change a series definition for scope SERIES (the whole segment) or
    FUTURE (from `cutoff`, an original slot date, on). `definition` is the
    series as it should be (same id); `expected_version` the stored version
    it was edited from. See the module docstring for the policies.
    """
    if scope == EditScope.OCCURRENCE:
        raise SeriesConfigurationError("use edit_occurrence for a single occurrence.")
    now = (clock or _utcnow)()
    with service.transaction():
        stored = _require(service, definition.id, expected_version, "task")
        if not stored.is_series or not definition.is_series:
            raise SeriesConfigurationError("only a recurring series definition can be edited for its series.")
        if not definition.recurrence.configured:
            if scope == EditScope.SERIES and stored.needs_configuration:
                return _edit_in_place(service, stored, definition, expected_version, now)  # content only, still unset
            raise SeriesConfigurationError("choose the series' start date and time zone.")
        if scope == EditScope.FUTURE and cutoff is None:
            raise SeriesConfigurationError("a change of this and every later occurrence needs the occurrence's date.")
        if scope == EditScope.SERIES and (stored.needs_configuration or _same_rule(stored, definition)):
            return _edit_in_place(service, stored, definition, expected_version, now)
        if stored.needs_configuration:
            raise SeriesConfigurationError("configure the series' start date and time zone (entire series) first.")
        start = stored.recurrence.start_date
        split_at = max(cutoff, start) if scope == EditScope.FUTURE else start
        return _split(service, stored, definition, expected_version, split_at, scope, now)


def _edit_in_place(service: PlanningService, stored: Task, definition: Task, expected_version: int,
                   now: datetime) -> SeriesChange:
    saved = service.update_task(definition, expected_version=expected_version)
    occurrences = service.occurrences_of_series([saved.id], include_deleted=False).get(saved.id, [])
    if len(occurrences) > MAX_CHANGE_OCCURRENCES:
        raise RecurrenceLimitError(f"this change would touch more than {MAX_CHANGE_OCCURRENCES} occurrences.")
    statuses = service.execution_statuses_for_tasks(task.id for task in occurrences)
    preserved: list[PreservedOccurrence] = []
    refresh: list[Task] = []
    expander = _Expander(service, budget=MAX_EXPANSION_OCCURRENCES, now=now)
    for occurrence in occurrences:
        if statuses.get(occurrence.id, set()) & HISTORY_PROTECTED_STATUSES:
            preserved.append(PreservedOccurrence(occurrence, "history"))
            continue
        if occurrence.occurrence_state == OccurrenceState.MODIFIED:
            preserved.append(PreservedOccurrence(occurrence, "modified"))
            continue
        dependencies = _refreshed_dependencies(expander, saved, occurrence)
        if dependencies is None:
            continue  # reported in expander.problems; the occurrence keeps its previous content
        refreshed = occurrence.model_copy(update={
            **{name: getattr(saved, name) for name in _CONTENT_FIELDS},
            "dependency_ids": dependencies, "series_version": saved.version,
        })
        if refreshed != occurrence:
            refresh.append(refreshed)
    if expander.created:
        service.materialize_occurrences(list(expander.created.values()))
    updated = service.refresh_occurrences(refresh, expected_versions={task.id: task.version for task in refresh}) \
        if refresh else []
    return SeriesChange(EditScope.SERIES, series=saved, updated=updated, preserved=preserved,
                        problems=expander.problems)


def _refreshed_dependencies(expander: _Expander, series: Task, occurrence: Task) -> list[uuid.UUID] | None:
    """The occurrence's dependencies under the series' current definition (None: a prerequisite is unresolved)."""
    if series.needs_configuration:
        return [dependency for dependency in series.dependency_ids
                if (target := expander.lineage.task(dependency)) is None or not target.is_series]
    dependencies: list[uuid.UUID] = []
    for dependency_id in series.dependency_ids:
        target = expander.lineage.task(dependency_id)
        if target is None or not target.is_series:
            dependencies.append(dependency_id)
            continue
        prerequisite, problem = expander._prerequisite(series, target, occurrence.occurrence_slot, 0)
        if problem is not None:
            expander.problems.append(SlotProblem(series.id, occurrence.occurrence_slot, "dependency_unresolved", problem))
            return None
        dependencies.append(prerequisite)
    return dependencies


def _split(service: PlanningService, stored: Task, definition: Task, expected_version: int, cutoff: date_,
           scope: EditScope, now: datetime) -> SeriesChange:
    old_rule = SeriesRule.of(stored.recurrence)
    recurrence = definition.recurrence
    if scope == EditScope.FUTURE:
        same_cadence = (recurrence.model_copy(update={"end_date": None, "count": None, "start_date": None})
                        == stored.recurrence.model_copy(update={"end_date": None, "count": None, "start_date": None}))
        count = recurrence.count
        if count is not None and same_cadence:
            count -= old_rule.slot_index(cutoff)  # the count keeps meaning "occurrences from the original start"
            if count <= 0:
                raise SeriesConfigurationError("the series has no occurrence on or after that date to change.")
        recurrence = recurrence.model_copy(update={"start_date": cutoff, "count": count})
    successor = definition.model_copy(update={
        "id": uuid.uuid4(), "user_id": stored.user_id, "recurrence": recurrence, "series_predecessor_id": stored.id,
        "created_at": now, "updated_at": now, "version": 1, "deleted_at": None,
    })
    ended = service.update_task(_ended(stored, cutoff), expected_version=expected_version)
    supersede, preserved = _split_existing(service, stored, cutoff)
    superseded = _supersede(service, supersede)
    created = service.create_task(successor)
    return SeriesChange(scope, series=ended, successor=created, superseded=superseded, preserved=preserved)


def delete_series(
    service: PlanningService,
    series_id: uuid.UUID,
    *,
    expected_version: int,
    scope: EditScope,
    cutoff: date_ | None = None,
) -> SeriesChange:
    """
    Delete the series' occurrences from `cutoff` on (FUTURE: the segment
    then ends the day before) or the entire segment (SERIES: the definition
    is tombstoned too). Preserved occurrences stay; see the module docstring.
    """
    if scope == EditScope.OCCURRENCE:
        raise SeriesConfigurationError("use delete_occurrence for a single occurrence.")
    with service.transaction():
        stored = _require(service, series_id, expected_version, "task")
        if not stored.is_series:
            raise SeriesConfigurationError("only a recurring series definition can be deleted for its series.")
        if scope == EditScope.FUTURE:
            if cutoff is None:
                raise SeriesConfigurationError("deleting this and every later occurrence needs the occurrence's date.")
            if stored.needs_configuration:
                raise SeriesConfigurationError("configure the series' start date and time zone first, or delete the "
                                               "entire series.")
            if cutoff > stored.recurrence.start_date:
                supersede, preserved = _split_existing(service, stored, cutoff)
                ended = service.update_task(_ended(stored, cutoff), expected_version=expected_version)
                superseded = _supersede(service, supersede)
                return SeriesChange(scope, series=ended, superseded=superseded, preserved=preserved)
        supersede, preserved = _split_existing(service, stored, date_.min)
        superseded = _supersede(service, supersede)
        service.delete_task(series_id, expected_version=expected_version)
        removed = service.get_tasks_including_deleted([series_id])[series_id]
    return SeriesChange(EditScope.SERIES if scope == EditScope.SERIES else scope, series=removed,
                        superseded=superseded, preserved=preserved)


def series_of(service: PlanningService, task: Task) -> Task | None:
    """The series definition an occurrence belongs to (tombstoned definitions included), or the task itself."""
    if task.is_series:
        return task
    if task.is_occurrence:
        return service.get_tasks_including_deleted([task.series_id]).get(task.series_id)
    return None


__all__ = [
    "EditScope", "ExpansionResult", "LegacyCollision", "MAX_CHANGE_OCCURRENCES", "MAX_EXPANSION_DAYS",
    "MAX_EXPANSION_OCCURRENCES", "PreservedOccurrence", "SeriesChange", "SlotProblem", "delete_occurrence",
    "delete_series", "edit_occurrence", "edit_series", "expand_occurrences", "occurrence_for", "series_of",
]
