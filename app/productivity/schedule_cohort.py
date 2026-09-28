"""
app/productivity/schedule_cohort.py

The schedule-cohort report (Milestone 5; the full contract is
docs/analytics.md): planned-versus-actual measures over the *intended
occurrences* of a reporting date range, including planned work that never
got an execution. Pure: it reads only a ScheduleHistory
(app/planning/history.py) and explicit parameters -- no database, no host
clock, no host timezone.

Parameters: an inclusive local date range [start_date, end_date] in an
explicit IANA reporting timezone, read as the half-open instant interval
[local midnight of start_date, local midnight after end_date), and an aware
`as_of` cutoff (the report describes the plan and the outcomes as they were
at that instant).

Occurrences. Placements linked by superseded_by_id (an explicit move or a
regeneration that re-placed the same occurrence) form one lineage; the
lineage is one intended occurrence and is counted once. Its *applicable*
placement is the one that was live at `as_of` (created at or before it, not
removed by it). An occurrence belongs to the range when its applicable
placement's planned start lies in the range; its state is that placement's
execution as of `as_of`, reconstructed from recorded instants:

    completed / skipped / cancelled   terminal, its final end at or before as_of
    in_progress / paused              started at or before as_of, not terminal by then
    not_started                       no execution, or none of the above by as_of

Due: the applicable planned end is at or before `as_of`. Future (not due)
occurrences are counted separately and never enter a due rate. The due
cohort excludes explicit (user) cancellations, reported separately:

    due completion rate = completed due occurrences / due non-cancelled occurrences
    due skip rate       = skipped due occurrences   / due non-cancelled occurrences

The denominator therefore includes overdue not-started ("unattempted"),
in-progress, paused and skipped occurrences. Nothing is ever completed
unless its execution says so and its completion is recorded by `as_of`.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from app.execution.models import ExecutionStatus
from app.planning.history import ExecutionHistory, ScheduleHistory
from app.planning.models import PlacementRemovalReason, ScheduledTask
from app.planning.time import elapsed_minutes, local_date_of, local_day_start_utc, validate_timezone
from app.productivity.buckets import time_bucket_for_instant
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, evidence_level_for_count

REPORT_VERSION = 1
#: The longest reporting range one report covers.
MAX_REPORT_DAYS = 366

#: Underestimation signal: at least this many paired completions ...
MIN_UNDERESTIMATION_SAMPLES = 5
#: ... of which at least this share ran longer than estimated ...
UNDERESTIMATED_SHARE = 0.7
#: ... with a median actual/estimate ratio of at least this.
UNDERESTIMATED_MEDIAN_RATIO = 1.1

#: Overload signal for a local date: at least this many planned minutes of due work unfinished ...
OVERLOAD_UNFINISHED_MINUTES = 120.0
#: ... and at least this share of the date's due planned minutes.
OVERLOAD_UNFINISHED_SHARE = 0.5


def utc(instant: datetime) -> datetime:
    return instant.astimezone(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None


# -----------------------------------------------------------------------------
# Output models
# -----------------------------------------------------------------------------


class OccurrenceState(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    NOT_STARTED = "not_started"


class Rate(BaseModel):
    """A ratio with its parts. `value` is None (unavailable) when the denominator is 0 -- never 0."""

    numerator: int
    denominator: int
    value: float | None
    evidence_level: EvidenceLevel
    unavailable_reason: str | None = None


def _rate(numerator: int, denominator: int, thresholds: ProductivityThresholds, empty: str) -> Rate:
    return Rate(
        numerator=numerator, denominator=denominator,
        value=round(numerator / denominator, 4) if denominator else None,
        evidence_level=evidence_level_for_count(denominator, thresholds),
        unavailable_reason=None if denominator else empty,
    )


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


class CohortOccurrence(BaseModel):
    """One intended occurrence of the report, as of the cutoff."""

    #: The applicable placement (live at as_of) and the lineage's first placement (the original plan).
    placement_id: uuid.UUID
    original_placement_id: uuid.UUID
    task_id: uuid.UUID
    #: The historical category (placement snapshot, else the execution's), or None: unknown.
    category: str | None
    category_source: str
    plan_timezone: str
    planned_start: datetime
    planned_end: datetime
    #: planned_start's date in the reporting timezone.
    local_date: date
    planned_minutes: float
    due: bool
    state: OccurrenceState
    execution_id: str | None = None
    #: The execution's own planned duration snapshot (its historical estimate), when there is an execution.
    estimate_minutes: float | None = None
    actual_active_minutes: float | None = None
    actual_first_start: datetime | None = None
    actual_final_end: datetime | None = None
    #: Explicit moves and regeneration replacements in its lineage, at or before as_of.
    reschedule_events: int = 0
    regeneration_events: int = 0


class OutcomeCounts(BaseModel):
    completed: int = 0
    skipped: int = 0
    in_progress: int = 0
    paused: int = 0
    #: Due, no execution started by as_of (untouched overdue work).
    overdue_unattempted: int = 0
    #: Explicit cancellations: excluded from the due denominator.
    cancelled: int = 0


class CohortBreakdown(BaseModel):
    """Due-completion over one slice of the due cohort (same definitions as the whole)."""

    due_completion: Rate
    due_skip: Rate
    outcomes: OutcomeCounts


class DurationComparison(BaseModel):
    """
    Completed occurrences with a known actual active duration, paired with
    their *historical* estimate (the execution's planned-duration snapshot).
    """

    pairs: int
    total_estimated_minutes: float
    total_actual_active_minutes: float
    #: actual - estimate, per pair.
    median_signed_error_minutes: float | None
    mean_absolute_error_minutes: float | None
    #: actual / estimate, over pairs with a positive estimate.
    median_ratio: float | None
    underestimated: int
    overestimated: int
    exact: int
    #: Completed occurrences left out: no recorded actual duration / a zero estimate (ratio only).
    missing_actual: int
    zero_estimate: int


class StartTiming(BaseModel):
    """Real instants: first start of work minus the applicable planned start (UTC arithmetic)."""

    known: int
    #: Occurrences whose work had not started by as_of, or whose start instant is not recorded.
    unknown: int
    median_signed_delay_minutes: float | None
    #: max(0, delay): being early is not negative lateness.
    median_lateness_minutes: float | None
    mean_lateness_minutes: float | None
    late_starts: int
    #: Completed after their planned end (and when: a completion late into another local date is still
    #: attributed to its planned date).
    completed_after_planned_end: int
    median_completion_lateness_minutes: float | None


class WorkloadMinutes(BaseModel):
    """Minutes, each with its own basis."""

    #: Planned intervals of the due cohort (non-cancelled due occurrences).
    due_scheduled_minutes: float
    #: Planned intervals of future occurrences in the range.
    future_scheduled_minutes: float
    #: Planned intervals of the completed due occurrences.
    completed_planned_minutes: float
    #: Actual active (session) minutes of completed due occurrences with a recorded actual duration.
    completed_actual_active_minutes: float
    completed_missing_actual: int


class RescheduleStats(BaseModel):
    #: Distinct occurrences of the range explicitly moved at least once / all occurrences of the range.
    reschedule_rate: Rate
    #: Explicit moves in those lineages (one occurrence can be moved several times).
    reschedule_events: int
    #: Automatic regeneration replacements, kept apart from explicit moves.
    regenerated_occurrences: int
    regeneration_events: int


class UnderestimationGroup(BaseModel):
    group_by: str  # "category" or "task"
    key: str
    label: str
    pairs: int
    underestimated: int
    share_underestimated: float | None
    median_signed_error_minutes: float | None
    median_ratio: float | None
    consistently_underestimated: bool
    evidence: str


class DaySignal(BaseModel):
    """
    An explainable workload signal for one local date -- evidence, not proof
    of its cause (a skip can be a sensible choice; illness is not recorded).
    """

    local_date: date
    due_occurrences: int
    due_planned_minutes: float
    unfinished_planned_minutes: float
    unfinished_share: float | None
    #: Available minutes after fixed blocks, when the historical day window is known; None = unknown.
    available_minutes: float | None
    demand_to_capacity: float | None
    capacity_note: str
    high_unfinished_workload: bool
    reasons: list[str]


class DataQuality(BaseModel):
    """Counts of missing or unknowable history -- never filled in."""

    category_unknown: int = 0
    terminal_time_unknown: int = 0
    completed_missing_actual: int = 0
    zero_estimates: int = 0
    #: Tombstones in the range removed from the plan (no successor) by the cutoff, by reason; "unknown" =
    #: removed before reasons were recorded (schema v7 / server 0007).
    removed_from_plan: dict[str, int] = Field(default_factory=dict)
    #: Placements planned in the range that were moved or re-placed outside it by the cutoff.
    moved_out_of_range: int = 0


class ScheduleCohortReport(BaseModel):
    report_version: int = REPORT_VERSION
    timezone: str
    start_date: date
    end_date: date
    range_start_utc: datetime
    range_end_utc: datetime
    as_of: datetime

    occurrence_count: int
    due_count: int
    future_count: int
    due_outcomes: OutcomeCounts
    #: Future occurrences by state (e.g. completed early).
    future_states: dict[str, int]

    due_completion: Rate
    due_skip: Rate
    reschedules: RescheduleStats
    duration: DurationComparison
    start_timing: StartTiming
    workload: WorkloadMinutes
    #: By historical category ("unknown" when no snapshot exists).
    by_category: dict[str, CohortBreakdown]
    #: By the planned start's local time-of-day (in each plan's own timezone).
    by_planned_time_bucket: dict[str, CohortBreakdown]
    #: A separate view: by the actual first start's local time-of-day, over due occurrences that started.
    by_actual_start_time_bucket: dict[str, CohortBreakdown]
    underestimation: list[UnderestimationGroup]
    day_signals: list[DaySignal]
    data_quality: DataQuality
    occurrences: list[CohortOccurrence]


# -----------------------------------------------------------------------------
# Building
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportWindow:
    timezone: str
    start_date: date
    end_date: date
    start_utc: datetime
    end_utc: datetime


def report_window(start_date: date, end_date: date, timezone_name: str) -> ReportWindow:
    """The half-open instant interval of an inclusive local date range (DST-safe, validated)."""
    validate_timezone(timezone_name)
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}.")
    if (end_date - start_date).days + 1 > MAX_REPORT_DAYS:
        raise ValueError(f"a report may cover at most {MAX_REPORT_DAYS} days.")
    return ReportWindow(
        timezone=timezone_name, start_date=start_date, end_date=end_date,
        start_utc=local_day_start_utc(start_date, timezone_name),
        end_utc=local_day_start_utc(end_date + timedelta(days=1), timezone_name),
    )


def _live_at(placement: ScheduledTask, as_of: datetime) -> bool:
    return placement.created_at <= as_of and (placement.deleted_at is None or placement.deleted_at > as_of)


def _removed_by(placement: ScheduledTask, as_of: datetime) -> bool:
    return placement.deleted_at is not None and placement.deleted_at <= as_of


def _lineages(placements: dict[uuid.UUID, ScheduledTask]) -> dict[uuid.UUID, list[ScheduledTask]]:
    """{root: its placements}; a root is a placement whose successor is not among `placements` (or none)."""
    def root_of(placement: ScheduledTask) -> uuid.UUID:
        seen = {placement.id}
        current = placement
        while current.superseded_by_id is not None and current.superseded_by_id in placements:
            current = placements[current.superseded_by_id]
            if current.id in seen:  # defensive: history never has cycles
                break
            seen.add(current.id)
        return current.id

    groups: dict[uuid.UUID, list[ScheduledTask]] = defaultdict(list)
    for placement in placements.values():
        groups[root_of(placement)].append(placement)
    return groups


@dataclass(frozen=True)
class _Execution:
    state: OccurrenceState
    first_start: datetime | None
    final_end: datetime | None
    terminal_time_unknown: bool


def _state_at(item: ExecutionHistory | None, as_of: datetime) -> _Execution:
    """The execution's lifecycle state at `as_of`, from its recorded instants (never from elapsed plan time)."""
    if item is None:
        return _Execution(OccurrenceState.NOT_STARTED, None, None, False)
    execution = item.execution
    starts = [(_parse(s.started_at), _parse(s.ended_at)) for s in item.sessions]
    first_start = execution.actual_first_start_at or (starts[0][0] if starts else None)
    final_end = execution.actual_final_end_at
    terminal = {ExecutionStatus.COMPLETED: OccurrenceState.COMPLETED, ExecutionStatus.SKIPPED: OccurrenceState.SKIPPED,
                ExecutionStatus.CANCELLED: OccurrenceState.CANCELLED}.get(execution.status)
    if terminal is not None and final_end is None:
        return _Execution(terminal, first_start, None, True)  # recorded outcome, time unknown
    if terminal is not None and final_end <= as_of:
        return _Execution(terminal, first_start, final_end, False)
    begun = [(start, end) for start, end in starts if start <= as_of]
    if not begun:
        return _Execution(OccurrenceState.NOT_STARTED, None, None, False)
    open_at_cutoff = begun[-1][1] is None or begun[-1][1] > as_of
    state = OccurrenceState.IN_PROGRESS if open_at_cutoff else OccurrenceState.PAUSED
    return _Execution(state, first_start if first_start is not None and first_start <= as_of else begun[0][0], None, False)


class ScheduleHistorySource(Protocol):
    """Anything that reads a ScheduleHistory in one owner-scoped snapshot (a PlanningService, a repository)."""

    def schedule_history(self, start_utc: datetime, end_utc: datetime) -> ScheduleHistory: ...


def read_schedule_cohort_report(
    source: ScheduleHistorySource,
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
    as_of: datetime,
    now: datetime,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> ScheduleCohortReport:
    """
    Read the range's history from `source` and build the report. `as_of` is
    the cutoff (aware); a cutoff after `now` is refused -- outcomes that have
    not happened yet cannot be reported.
    """
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError("as_of and now must be aware instants")
    if as_of > now:
        raise ValueError("as_of cannot be later than the current time.")
    window = report_window(start_date, end_date, timezone_name)
    return build_schedule_cohort_report(
        source.schedule_history(window.start_utc, window.end_utc), window, as_of=as_of, thresholds=thresholds
    )


def read_schedule_history_and_report(
    source: ScheduleHistorySource,
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
    as_of: datetime,
    now: datetime,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> tuple[ScheduleHistory, ScheduleCohortReport]:
    """read_schedule_cohort_report, also returning the history it was built from (history browsing)."""
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError("as_of and now must be aware instants")
    if as_of > now:
        raise ValueError("as_of cannot be later than the current time.")
    window = report_window(start_date, end_date, timezone_name)
    history = source.schedule_history(window.start_utc, window.end_utc)
    return history, build_schedule_cohort_report(history, window, as_of=as_of, thresholds=thresholds)


def lineage(history: ScheduleHistory, placement_id: uuid.UUID) -> list[ScheduledTask]:
    """The placement and everything it superseded, oldest plan first (following superseded_by_id back)."""
    placements = history.placements
    predecessors: dict[uuid.UUID, list[ScheduledTask]] = defaultdict(list)
    for placement in placements.values():
        if placement.superseded_by_id is not None:
            predecessors[placement.superseded_by_id].append(placement)
    chain, frontier, seen = [], [placement_id], set()
    while frontier:
        current = frontier.pop()
        if current in seen or current not in placements:
            continue
        seen.add(current)
        chain.append(placements[current])
        frontier.extend(p.id for p in predecessors[current])
    return sorted(chain, key=lambda p: (p.deleted_at is None, p.deleted_at or p.created_at, str(p.id)))


def build_schedule_cohort_report(
    history: ScheduleHistory,
    window: ReportWindow,
    *,
    as_of: datetime,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> ScheduleCohortReport:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be an aware instant")
    as_of = utc(as_of)
    if (history.start_utc, history.end_utc) != (window.start_utc, window.end_utc):
        raise ValueError("the history was read for another range")

    quality = DataQuality()
    removed: dict[str, int] = defaultdict(int)
    occurrences: list[CohortOccurrence] = []

    for links in _lineages(dict(history.placements)).values():
        in_range = [p for p in links if window.start_utc <= utc(p.planned_start) < window.end_utc]
        live = sorted((p for p in links if _live_at(p, as_of)), key=lambda p: (p.created_at, str(p.id)))
        applicable = live[-1] if live else None
        if applicable is None or applicable not in in_range:
            for placement in in_range:  # planned here, but not this range's occurrence at the cutoff
                if not _removed_by(placement, as_of):
                    continue  # created after the cutoff: not yet planned then
                if placement.superseded_by_id is not None:
                    quality.moved_out_of_range += 1
                else:
                    removed[placement.removal_reason.value if placement.removal_reason else "unknown"] += 1
            continue
        occurrences.append(_occurrence(applicable, links, history.executions.get(applicable.id), window, as_of, quality))

    quality.removed_from_plan = dict(sorted(removed.items()))
    occurrences.sort(key=lambda o: (o.planned_start, str(o.placement_id)))
    return _aggregate(occurrences, window, as_of, thresholds, quality)


def _occurrence(
    applicable: ScheduledTask, links: list[ScheduledTask], item: ExecutionHistory | None, window: ReportWindow,
    as_of: datetime, quality: DataQuality,
) -> CohortOccurrence:
    state = _state_at(item, as_of)
    if state.terminal_time_unknown:
        quality.terminal_time_unknown += 1
    execution = item.execution if item is not None else None
    if applicable.task_category is not None:
        category, source = applicable.task_category, "placement_snapshot"
    elif execution is not None:
        category, source = execution.category, "execution_snapshot"
    else:
        category, source = None, "unknown"
        quality.category_unknown += 1
    actual = execution.actual_active_duration_minutes if state.state == OccurrenceState.COMPLETED else None
    moves = [p for p in links if _removed_by(p, as_of) and p.superseded_by_id is not None]
    return CohortOccurrence(
        placement_id=applicable.id,
        original_placement_id=_original(links).id,
        task_id=applicable.task_id, category=category, category_source=source,
        plan_timezone=applicable.timezone, planned_start=utc(applicable.planned_start),
        planned_end=utc(applicable.planned_end), local_date=local_date_of(applicable.planned_start, window.timezone),
        planned_minutes=round(elapsed_minutes(applicable.planned_start, applicable.planned_end), 2),
        due=utc(applicable.planned_end) <= as_of, state=state.state,
        execution_id=execution.id if execution is not None else None,
        estimate_minutes=float(execution.planned_duration) if execution is not None else None,
        actual_active_minutes=actual,
        actual_first_start=utc(state.first_start) if state.first_start is not None else None,
        actual_final_end=utc(state.final_end) if state.final_end is not None else None,
        reschedule_events=sum(1 for p in moves if p.removal_reason == PlacementRemovalReason.RESCHEDULED),
        regeneration_events=sum(1 for p in moves if p.removal_reason == PlacementRemovalReason.REGENERATED),
    )


def _original(links: list[ScheduledTask]) -> ScheduledTask:
    """The start of a lineage: a placement that supersedes nothing (by structure, not by timestamps)."""
    superseding = {p.superseded_by_id for p in links}
    starts = [p for p in links if p.id not in superseding] or links
    return min(starts, key=lambda p: (utc(p.planned_start), str(p.id)))


def _outcomes(occurrences: list[CohortOccurrence]) -> OutcomeCounts:
    counts = OutcomeCounts()
    for occurrence in occurrences:
        if occurrence.state == OccurrenceState.NOT_STARTED:
            counts.overdue_unattempted += 1
        else:
            setattr(counts, occurrence.state.value, getattr(counts, occurrence.state.value) + 1)
    return counts


def _breakdown(occurrences: list[CohortOccurrence], thresholds: ProductivityThresholds) -> CohortBreakdown:
    outcomes = _outcomes(occurrences)
    denominator = len(occurrences) - outcomes.cancelled
    empty = "no due, non-cancelled occurrences in this slice"
    return CohortBreakdown(
        due_completion=_rate(outcomes.completed, denominator, thresholds, empty),
        due_skip=_rate(outcomes.skipped, denominator, thresholds, empty), outcomes=outcomes,
    )


def _grouped(occurrences: list[CohortOccurrence], key, thresholds) -> dict[str, CohortBreakdown]:
    groups: dict[str, list[CohortOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        groups[key(occurrence)].append(occurrence)
    return {name: _breakdown(groups[name], thresholds) for name in sorted(groups)}


def _aggregate(
    occurrences: list[CohortOccurrence], window: ReportWindow, as_of: datetime, thresholds: ProductivityThresholds,
    quality: DataQuality,
) -> ScheduleCohortReport:
    due = [o for o in occurrences if o.due]
    future = [o for o in occurrences if not o.due]
    cohort = [o for o in due if o.state != OccurrenceState.CANCELLED]
    overall = _breakdown(due, thresholds)

    completed = [o for o in occurrences if o.state == OccurrenceState.COMPLETED]
    quality.completed_missing_actual = sum(1 for o in completed if o.actual_active_minutes is None)
    pairs = [o for o in completed if o.actual_active_minutes is not None and o.estimate_minutes is not None]
    quality.zero_estimates = sum(1 for o in pairs if o.estimate_minutes <= 0)
    errors = [o.actual_active_minutes - o.estimate_minutes for o in pairs]
    ratios = [o.actual_active_minutes / o.estimate_minutes for o in pairs if o.estimate_minutes > 0]
    duration = DurationComparison(
        pairs=len(pairs), total_estimated_minutes=round(sum(o.estimate_minutes for o in pairs), 2),
        total_actual_active_minutes=round(sum(o.actual_active_minutes for o in pairs), 2),
        median_signed_error_minutes=_median(errors),
        mean_absolute_error_minutes=round(statistics.mean(abs(e) for e in errors), 2) if errors else None,
        median_ratio=round(statistics.median(ratios), 3) if ratios else None,
        underestimated=sum(1 for e in errors if e > 0), overestimated=sum(1 for e in errors if e < 0),
        exact=sum(1 for e in errors if e == 0),
        missing_actual=sum(1 for o in completed if o.actual_active_minutes is None),
        zero_estimate=quality.zero_estimates,
    )

    delays = [elapsed_minutes(o.planned_start, o.actual_first_start) for o in occurrences
              if o.actual_first_start is not None]
    lateness = [max(0.0, d) for d in delays]
    finished_late = [elapsed_minutes(o.planned_end, o.actual_final_end) for o in completed
                     if o.actual_final_end is not None and o.actual_final_end > o.planned_end]
    start_timing = StartTiming(
        known=len(delays), unknown=len(occurrences) - len(delays),
        median_signed_delay_minutes=_median(delays), median_lateness_minutes=_median(lateness),
        mean_lateness_minutes=round(statistics.mean(lateness), 2) if lateness else None,
        late_starts=sum(1 for d in delays if d > 0), completed_after_planned_end=len(finished_late),
        median_completion_lateness_minutes=_median(finished_late),
    )

    done = [o for o in cohort if o.state == OccurrenceState.COMPLETED]
    workload = WorkloadMinutes(
        due_scheduled_minutes=round(sum(o.planned_minutes for o in cohort), 2),
        future_scheduled_minutes=round(sum(o.planned_minutes for o in future
                                           if o.state != OccurrenceState.CANCELLED), 2),
        completed_planned_minutes=round(sum(o.planned_minutes for o in done), 2),
        completed_actual_active_minutes=round(sum(o.actual_active_minutes for o in done
                                                  if o.actual_active_minutes is not None), 2),
        completed_missing_actual=sum(1 for o in done if o.actual_active_minutes is None),
    )

    moved = [o for o in occurrences if o.reschedule_events]
    regenerated = [o for o in occurrences if o.regeneration_events]
    reschedules = RescheduleStats(
        reschedule_rate=_rate(len(moved), len(occurrences), thresholds, "no occurrences in the range"),
        reschedule_events=sum(o.reschedule_events for o in occurrences),
        regenerated_occurrences=len(regenerated), regeneration_events=sum(o.regeneration_events for o in occurrences),
    )

    started = [o for o in due if o.actual_first_start is not None]
    return ScheduleCohortReport(
        timezone=window.timezone, start_date=window.start_date, end_date=window.end_date,
        range_start_utc=window.start_utc, range_end_utc=window.end_utc, as_of=as_of,
        occurrence_count=len(occurrences), due_count=len(due), future_count=len(future),
        due_outcomes=overall.outcomes,
        future_states={state: sum(1 for o in future if o.state.value == state)
                       for state in sorted({o.state.value for o in future})},
        due_completion=overall.due_completion, due_skip=overall.due_skip, reschedules=reschedules,
        duration=duration, start_timing=start_timing, workload=workload,
        by_category=_grouped(due, lambda o: o.category or "unknown", thresholds),
        by_planned_time_bucket=_grouped(
            due, lambda o: time_bucket_for_instant(o.planned_start, o.plan_timezone).value, thresholds),
        by_actual_start_time_bucket=_grouped(
            started, lambda o: time_bucket_for_instant(o.actual_first_start, o.plan_timezone).value, thresholds),
        underestimation=_underestimation(pairs),
        day_signals=_day_signals(due),
        data_quality=quality,
        occurrences=occurrences,
    )


def _underestimation(pairs: list[CohortOccurrence]) -> list[UnderestimationGroup]:
    """
    Groups are a historical category or one task identity (task_id) -- never a
    display name, so renamed tasks stay together and unrelated tasks that share
    a name stay apart. Only pairs with a positive estimate count.
    """
    positive = [o for o in pairs if o.estimate_minutes > 0]
    groups: dict[tuple[str, str], list[CohortOccurrence]] = defaultdict(list)
    for occurrence in positive:
        if occurrence.category is not None:
            groups[("category", occurrence.category)].append(occurrence)
        groups[("task", str(occurrence.task_id))].append(occurrence)
    result = []
    for (group_by, key) in sorted(groups):
        members = groups[(group_by, key)]
        errors = [o.actual_active_minutes - o.estimate_minutes for o in members]
        ratios = [o.actual_active_minutes / o.estimate_minutes for o in members]
        under = sum(1 for e in errors if e > 0)
        share = round(under / len(members), 4)
        median_ratio = round(statistics.median(ratios), 3)
        enough = len(members) >= MIN_UNDERESTIMATION_SAMPLES
        result.append(UnderestimationGroup(
            group_by=group_by, key=key, label=key if group_by == "category" else f"task {key}",
            pairs=len(members), underestimated=under, share_underestimated=share,
            median_signed_error_minutes=_median(errors), median_ratio=median_ratio,
            consistently_underestimated=enough and share >= UNDERESTIMATED_SHARE
            and median_ratio >= UNDERESTIMATED_MEDIAN_RATIO,
            evidence="sufficient" if enough else f"insufficient: {len(members)} of {MIN_UNDERESTIMATION_SAMPLES} "
                                                  "completions needed",
        ))
    return result


def _day_signals(due: list[CohortOccurrence]) -> list[DaySignal]:
    by_date: dict[date, list[CohortOccurrence]] = defaultdict(list)
    for occurrence in due:
        if occurrence.state != OccurrenceState.CANCELLED:
            by_date[occurrence.local_date].append(occurrence)
    signals = []
    for day in sorted(by_date):
        members = by_date[day]
        planned = round(sum(o.planned_minutes for o in members), 2)
        unfinished = round(sum(o.planned_minutes for o in members if o.state != OccurrenceState.COMPLETED), 2)
        share = round(unfinished / planned, 4) if planned else None
        high = unfinished >= OVERLOAD_UNFINISHED_MINUTES and share is not None and share >= OVERLOAD_UNFINISHED_SHARE
        reasons = [f"{unfinished:g} of {planned:g} due planned minutes were not completed "
                   f"(thresholds: {OVERLOAD_UNFINISHED_MINUTES:g} minutes and {OVERLOAD_UNFINISHED_SHARE:.0%})"] \
            if high else []
        signals.append(DaySignal(
            local_date=day, due_occurrences=len(members), due_planned_minutes=planned,
            unfinished_planned_minutes=unfinished, unfinished_share=share, available_minutes=None,
            demand_to_capacity=None,
            capacity_note="unknown: the day window in effect on that date is not recorded in history, and today's "
                          "preferences are not used to reconstruct it",
            high_unfinished_workload=high, reasons=reasons,
        ))
    return signals
