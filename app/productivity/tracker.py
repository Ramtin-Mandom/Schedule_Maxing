"""
app/productivity/tracker.py

The productivity tracker (docs/productivity-redesign-plan.md, contracts B-E):
awards, averages, per-type breakdowns and time views, computed from
authoritative records every time -- there is no stored counter or award
ledger, so a reopened or deleted completion simply stops counting and a
re-completion counts once.

Three date bases, never mixed (docs/analytics.md):

    execution-created   the existing terminal-outcome statistics
                        (ProductivityService.generate_report/build_dashboard);
                        not computed here.
    planned-date        counts and due rates: an intended occurrence (one
                        placement lineage, app/productivity/schedule_cohort.py)
                        belongs to the local date of its applicable planned
                        start, exactly once.
    completion-date     earned points and completed activity: a live completed
                        execution belongs to the local date of its recorded
                        completion instant (actual_final_end_at), whatever its
                        planned date and whether or not its plan still exists.

Due completion = due completed / (due completed + skipped + not started +
in progress + paused). Cancelled, future and removed occurrences are excluded
and reported separately; a zero denominator is unavailable, never 0%.

Points are the execution's own snapshot (never a placement's optimizer
score, never the task's current value). A completion without a snapshot is
counted with unknown points, and an award that could be affected says so.

Reading (read_tracker_data) walks the whole recorded history in bounded
windows (at most MAX_REPORT_DAYS each), with one planned-history read and
one completion read per window -- never a query per record. Occurrences and
completions are merged as raw records, so every median is exact, an
occurrence is counted once however many windows mention its lineage, and a
streak can cross any window boundary.

Building (build_tracker_report) is pure: explicit records, reporting
timezone and `as_of`; no storage, no host clock, no host timezone.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, Field

from app.planning.history import CompletionHistory, HistoryBounds, ScheduleHistory, historical_plan
from app.planning.time import local_date_of, validate_timezone
from app.productivity.buckets import TimeBucket, day_of_week_for_date, time_bucket_for_instant
from app.productivity.day_summary import DayStatusClass, classify_day
from app.productivity.schedule_cohort import (
    MAX_REPORT_DAYS,
    OccurrenceState,
    Rate,
    ScheduleCohortReport,
    build_schedule_cohort_report,
    report_window,
    utc,
)
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, evidence_level_for_count

TRACKER_VERSION = 1
#: The furthest back one tracker report reads. Older history is reported as not read, never silently dropped.
MAX_HISTORY_DAYS = MAX_REPORT_DAYS * 30

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
BUCKETS = tuple(bucket.value for bucket in (TimeBucket.MORNING, TimeBucket.AFTERNOON, TimeBucket.EVENING,
                                            TimeBucket.NIGHT))
#: The day classes that count as a green day (app/productivity/day_summary.py: >=60% and >=80% completed).
GREEN_CLASSES = frozenset({DayStatusClass.MOSTLY_COMPLETED, DayStatusClass.MOSTLY_COMPLETED_STRONG})
_PENDING = frozenset({OccurrenceState.NOT_STARTED, OccurrenceState.IN_PROGRESS, OccurrenceState.PAUSED})

PLANNED_BASIS = "planned date"
COMPLETION_BASIS = "completion date"


# -----------------------------------------------------------------------------
# Input records
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedItem:
    """One intended occurrence on the planned-date basis, with its historical facts."""

    placement_id: uuid.UUID
    task_id: uuid.UUID
    local_date: date
    planned_start: datetime
    bucket: str
    planned_minutes: float
    due: bool
    state: OccurrenceState
    execution_id: str | None
    name: str | None
    category: str | None
    tags: tuple[str, ...] | None
    type_id: uuid.UUID | None
    estimate_minutes: float | None
    actual_active_minutes: float | None
    start_delay_minutes: float | None


@dataclass(frozen=True)
class CompletionItem:
    """One live completed execution on the completion-date basis."""

    execution_id: str
    occurrence_key: str
    task_id: uuid.UUID | None
    placement_id: uuid.UUID | None
    completed_at: datetime
    local_date: date
    points: int | None
    active_minutes: float | None
    name: str | None
    category: str | None
    tags: tuple[str, ...] | None
    type_id: uuid.UUID | None
    #: The planned start's weekday and bucket when the work was planned (None: no plan is recorded).
    planned_weekday: str | None = None
    planned_bucket: str | None = None


@dataclass
class TrackerData:
    """Everything one report is built from (read_tracker_data, or built directly in tests)."""

    planned: list[PlannedItem] = field(default_factory=list)
    completions: list[CompletionItem] = field(default_factory=list)
    type_labels: dict[uuid.UUID, str] = field(default_factory=dict)
    #: {local date: tombstones planned there that were removed without a successor / moved to another date}.
    removed_by_date: dict[date, int] = field(default_factory=dict)
    moved_out_by_date: dict[date, int] = field(default_factory=dict)
    history_truncated: bool = False
    lineage_truncated: bool = False
    unknown_completion_dates: int = 0
    duplicate_completions: int = 0
    windows_read: int = 0


class TrackerFilters(BaseModel):
    """The main filters. Category and tag are historical snapshots; they never merge or split a type."""

    category: str | None = None
    tag: str | None = None
    weekday: str | None = None
    time_bucket: str | None = None

    @property
    def active(self) -> bool:
        return any(value is not None for value in (self.category, self.tag, self.weekday, self.time_bucket))


# -----------------------------------------------------------------------------
# Output models
# -----------------------------------------------------------------------------


class StatusCounts(BaseModel):
    """
    Occurrences on the planned-date basis. The due parts are disjoint and add
    up to `due_denominator`; unresolved (not started + in progress + paused,
    due or not) and overdue_not_started (its due, not-started subset) overlap
    them and each other -- they are views, not further addends.
    """

    planned: int = 0
    completed: int = 0
    skipped: int = 0
    cancelled: int = 0
    in_progress: int = 0
    paused: int = 0
    not_started: int = 0
    unresolved: int = 0
    future: int = 0
    overdue_not_started: int = 0
    due_completed: int = 0
    due_skipped: int = 0
    due_in_progress: int = 0
    due_paused: int = 0
    due_denominator: int = 0
    due_completion: Rate
    due_skip: Rate


class ActivityTotals(BaseModel):
    """Completed activity on the completion-date basis."""

    completions: int = 0
    #: The sum of the known points snapshots, how many completions had one, and how many did not.
    known_points: int = 0
    known_point_completions: int = 0
    unknown_point_completions: int = 0
    productive_minutes: float = 0.0
    timed_completions: int = 0


class DurationStats(BaseModel):
    """Completed occurrences with both a historical estimate and a recorded active duration (planned-date basis)."""

    pairs: int = 0
    median_estimated_minutes: float | None = None
    median_actual_minutes: float | None = None
    mean_absolute_error_minutes: float | None = None
    total_estimated_minutes: float = 0.0
    total_actual_minutes: float = 0.0


class AwardWinner(BaseModel):
    key: str
    label: str
    start_date: date | None = None
    end_date: date | None = None
    value: float
    detail: dict[str, float | int | str | None] = Field(default_factory=dict)
    #: The records the value was computed from (for the drill-down).
    execution_ids: list[str] = Field(default_factory=list)
    placement_ids: list[uuid.UUID] = Field(default_factory=list)


class Award(BaseModel):
    kind: str
    title: str
    available: bool
    unavailable_reason: str | None = None
    #: True when missing history could change the result; `qualifications` says what is missing.
    qualified: bool = False
    qualifications: list[str] = Field(default_factory=list)
    basis: str
    calculation: str
    unit: str
    value: float | None = None
    #: Every tie, earliest (or alphabetically first) first; winners[0] is the representative.
    winners: list[AwardWinner] = Field(default_factory=list)
    tie_count: int = 0
    #: How many candidates (days, weeks, types) were compared.
    sample_count: int = 0
    #: The current, unfinished period -- shown beside the record, never as the record.
    partial: AwardWinner | None = None


class AverageSet(BaseModel):
    periods: int
    completed_tasks: float
    points: float
    productive_minutes: float


class Averages(BaseModel):
    available: bool
    unavailable_reason: str | None = None
    #: The first planned-or-completed activity in the selected range, and the last finished day (yesterday).
    first_activity_date: date | None = None
    through_date: date | None = None
    elapsed_days: int = 0
    complete_weeks: int = 0
    #: Per elapsed calendar day (zero-work days included) / per complete Monday-Sunday week (None: no such week).
    daily: AverageSet | None = None
    weekly: AverageSet | None = None
    totals: ActivityTotals = Field(default_factory=ActivityTotals)
    #: The mean of the daily due-completion rates over days with a non-zero due denominator (unweighted).
    average_daily_due_completion: float | None = None
    due_days: int = 0
    #: All due work of the same days pooled into one rate.
    pooled_due_completion: Rate
    today: ActivityTotals = Field(default_factory=ActivityTotals)
    current_week: ActivityTotals = Field(default_factory=ActivityTotals)


class DayView(BaseModel):
    local_date: date
    weekday: str
    #: Today, or a later date: its figures are not final.
    partial: bool
    counts: StatusCounts
    status_class: DayStatusClass
    activity: ActivityTotals
    removed: int = 0
    moved_out: int = 0
    placement_ids: list[uuid.UUID] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)


class PeriodView(BaseModel):
    """A calendar week (Monday-Sunday) or month of the selected range."""

    key: str
    start_date: date
    end_date: date
    #: False when the range or today cuts the period short.
    complete: bool
    days_in_range: int
    counts: StatusCounts
    activity: ActivityTotals
    green_days: int


class WeekdayView(BaseModel):
    weekday: str
    counts: StatusCounts
    activity: ActivityTotals
    #: Elapsed calendar dates of this weekday from the first activity through yesterday (zero-point dates included).
    eligible_days: int
    average_points_per_eligible_day: float | None


class BucketView(BaseModel):
    bucket: str
    counts: StatusCounts


class RankedGroup(BaseModel):
    available: bool
    unavailable_reason: str | None = None
    basis: str
    #: Every tie, in calendar order.
    winners: list[str] = Field(default_factory=list)
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    detail: dict[str, float | int | None] = Field(default_factory=dict)


class SupportedSlot(BaseModel):
    """The planned-start bucket with the largest completed-duration sample -- most evidence, not best performance."""

    key: str
    label: str
    buckets: list[str]
    sample_count: int
    evidence_level: EvidenceLevel
    note: str


class PlannedVsActual(BaseModel):
    key: str
    label: str
    durations: DurationStats


class PeriodTotals(BaseModel):
    start_date: date
    end_date: date
    counts: StatusCounts
    activity: ActivityTotals


class RecentComparison(BaseModel):
    recent: PeriodTotals
    baseline: PeriodTotals


class TypePeriod(BaseModel):
    period: str
    start_date: date | None
    end_date: date
    counts: StatusCounts
    activity: ActivityTotals
    durations: DurationStats
    productive_minutes: float


class TypeView(BaseModel):
    #: None: occurrences and completions whose type is unknown (never merged into a real type).
    type_id: uuid.UUID | None
    label: str
    #: Keyed "today", "week", "month", "all_time".
    periods: dict[str, TypePeriod]
    best_weekday: RankedGroup
    supported_slot: SupportedSlot | None
    placement_ids: list[uuid.UUID] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)


class Completeness(BaseModel):
    complete: bool
    notes: list[str] = Field(default_factory=list)
    history_truncated: bool = False
    lineage_truncated: bool = False
    unknown_completion_dates: int = 0
    unknown_points: int = 0
    unknown_type_occurrences: int = 0
    unknown_type_completions: int = 0
    duplicate_completions: int = 0
    removed_from_plan: int = 0
    moved_out: int = 0
    windows_read: int = 0


class RecordRef(BaseModel):
    """One contributing record, for a drill-down: a planned occurrence or a completion (historical facts only)."""

    kind: str  # "occurrence" or "completion"
    id: str
    name: str | None
    category: str | None
    type_id: uuid.UUID | None
    local_date: date
    #: An occurrence's state as of the cutoff and whether it was due; None for a completion.
    state: str | None = None
    due: bool | None = None
    planned_start: datetime | None = None
    completed_at: datetime | None = None
    points: int | None = None
    active_minutes: float | None = None


class GeneralSection(BaseModel):
    counts: StatusCounts
    activity: ActivityTotals
    durations: DurationStats
    #: First start minus planned start over occurrences that started (negative = early).
    median_start_delay_minutes: float | None
    start_delay_samples: int
    highest_point_day: Award
    best_week: Award
    most_completed_type: Award
    longest_green_streak: Award
    current_green_streak: Award
    averages: Averages


class TimeSection(BaseModel):
    days: list[DayView]
    weeks: list[PeriodView]
    months: list[PeriodView]
    weekdays: list[WeekdayView]
    buckets: list[BucketView]
    highest_completion_weekday: RankedGroup
    highest_points_weekday: RankedGroup
    supported_slots_by_category: list[SupportedSlot]
    supported_slots_by_type: list[SupportedSlot]
    planned_vs_actual_by_category: list[PlannedVsActual]
    planned_vs_actual_by_type: list[PlannedVsActual]
    recent: RecentComparison


class TrackerReport(BaseModel):
    tracker_version: int = TRACKER_VERSION
    timezone: str
    as_of: datetime
    today: date
    #: None: all time (from the first recorded activity).
    range_days: int | None
    range_start: date | None
    range_end: date
    filters: TrackerFilters
    completeness: Completeness
    general: GeneralSection
    types: list[TypeView]
    time: TimeSection
    #: Every record an id in this report refers to: {"p:<placement id>" | "e:<execution id>": its reference}.
    records: dict[str, RecordRef] = Field(default_factory=dict)
    type_labels: dict[str, str] = Field(default_factory=dict)
    #: Every category and tag of the recorded history, whatever the filters -- the choices a filter can offer.
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Small pure helpers
# -----------------------------------------------------------------------------


def week_start(day: date) -> date:
    """The Monday of the day's calendar week."""
    return day - timedelta(days=day.weekday())


def month_end(day: date) -> date:
    first_of_next = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_of_next - timedelta(days=1)


def _days(start: date, end: date) -> Iterable[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def _rate(numerator: int, denominator: int, thresholds: ProductivityThresholds, empty: str) -> Rate:
    return Rate(
        numerator=numerator, denominator=denominator,
        value=round(numerator / denominator, 4) if denominator else None,
        evidence_level=evidence_level_for_count(denominator, thresholds),
        unavailable_reason=None if denominator else empty,
    )


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _counts(items: Iterable[PlannedItem], thresholds: ProductivityThresholds) -> StatusCounts:
    tally: dict[str, int] = defaultdict(int)
    for item in items:
        tally["planned"] += 1
        tally[item.state.value] += 1
        if item.state in _PENDING:
            tally["unresolved"] += 1
        if not item.due:
            tally["future"] += 1
            continue
        if item.state == OccurrenceState.NOT_STARTED:
            tally["overdue_not_started"] += 1
        elif item.state != OccurrenceState.CANCELLED:
            tally[f"due_{item.state.value}"] += 1
    denominator = (tally["due_completed"] + tally["due_skipped"] + tally["overdue_not_started"]
                   + tally["due_in_progress"] + tally["due_paused"])
    empty = "no due, non-cancelled occurrences"
    return StatusCounts(
        **{name: tally[name] for name in ("planned", "completed", "skipped", "cancelled", "in_progress", "paused",
                                          "not_started", "unresolved", "future", "overdue_not_started",
                                          "due_completed", "due_skipped", "due_in_progress", "due_paused")},
        due_denominator=denominator,
        due_completion=_rate(tally["due_completed"], denominator, thresholds, empty),
        due_skip=_rate(tally["due_skipped"], denominator, thresholds, empty),
    )


def _activity(items: Iterable[CompletionItem]) -> ActivityTotals:
    totals = ActivityTotals()
    minutes = 0.0
    for item in items:
        totals.completions += 1
        if item.points is None:
            totals.unknown_point_completions += 1
        else:
            totals.known_points += item.points
            totals.known_point_completions += 1
        if item.active_minutes is not None:
            minutes += item.active_minutes
            totals.timed_completions += 1
    totals.productive_minutes = round(minutes, 2)
    return totals


def _durations(items: Iterable[PlannedItem]) -> DurationStats:
    pairs = [item for item in items if item.state == OccurrenceState.COMPLETED
             and item.actual_active_minutes is not None and item.estimate_minutes is not None]
    if not pairs:
        return DurationStats()
    estimates = [item.estimate_minutes for item in pairs]
    actuals = [item.actual_active_minutes for item in pairs]
    return DurationStats(
        pairs=len(pairs), median_estimated_minutes=_median(estimates), median_actual_minutes=_median(actuals),
        mean_absolute_error_minutes=round(statistics.mean(abs(a - e) for a, e in zip(actuals, estimates)), 2),
        total_estimated_minutes=round(sum(estimates), 2), total_actual_minutes=round(sum(actuals), 2),
    )


def _day_class(items: Iterable[PlannedItem]) -> tuple[DayStatusClass, int]:
    """(the shared day classification, how many occurrences are still pending) of one date's occurrences."""
    completed = uncompleted = pending = 0
    for item in items:
        if item.state == OccurrenceState.COMPLETED:
            completed += 1
        elif item.state in (OccurrenceState.SKIPPED, OccurrenceState.CANCELLED):
            uncompleted += 1  # the calendar classification counts a cancelled attempt as uncompleted
        else:
            pending += 1
    return classify_day(completed, uncompleted, pending), pending


def _matches(filters: TrackerFilters, category, tags, weekday, bucket) -> bool:
    if filters.category is not None and category != filters.category:
        return False
    if filters.tag is not None and (tags is None or filters.tag not in tags):
        return False
    if filters.weekday is not None and weekday != filters.weekday:
        return False
    return filters.time_bucket is None or bucket == filters.time_bucket


# -----------------------------------------------------------------------------
# Green-day streaks
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Streak:
    start: date
    end: date

    @property
    def length(self) -> int:
        return (self.end - self.start).days + 1


def green_streaks(
    classes: dict[date, tuple[DayStatusClass, int]], start: date, today: date
) -> tuple[list[Streak], Streak | None, bool]:
    """
    (every run of consecutive green days, the current run, whether today is
    provisional) over [start, today]. A day is green when classify_day says
    >=60% of its scheduled occurrences are completed. An empty day and a
    finished non-green day end a run. Today is provisional while it has no
    occurrences or some are unresolved: it neither extends nor ends the run
    that reached yesterday. Dates after today are never looked at.
    """
    runs: list[Streak] = []
    run_start: date | None = None
    last: date | None = None
    provisional = False
    for day in _days(start, today):
        status, pending = classes.get(day, (DayStatusClass.NO_TASKS, 0))
        if day == today and (status == DayStatusClass.NO_TASKS or pending):
            provisional = True
            break
        if status in GREEN_CLASSES:
            run_start, last = run_start or day, day
            continue
        if run_start is not None:
            runs.append(Streak(run_start, last))
        run_start = last = None
    current = Streak(run_start, last) if run_start is not None else None
    if current is not None:
        runs.append(current)
    return runs, current, provisional


# -----------------------------------------------------------------------------
# Building the report
# -----------------------------------------------------------------------------


def build_tracker_report(
    data: TrackerData,
    *,
    timezone_name: str,
    as_of: datetime,
    range_days: int | None = None,
    filters: TrackerFilters | None = None,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> TrackerReport:
    """
    The tracker report as of `as_of` in `timezone_name`. `range_days` selects
    the last N local dates ending today (None: all time, from the first
    recorded activity); the four per-type periods and the current streak are
    independent of it. Pure and deterministic: equal inputs give equal output.
    """
    validate_timezone(timezone_name)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be an aware instant")
    if range_days is not None and range_days <= 0:
        raise ValueError("range_days must be positive (or None for all time)")
    as_of = utc(as_of)
    today = local_date_of(as_of, timezone_name)
    filters = filters or TrackerFilters()

    planned_all = sorted(
        (item for item in data.planned
         if _matches(filters, item.category, item.tags, day_of_week_for_date(item.local_date), item.bucket)),
        key=lambda item: (item.planned_start, str(item.placement_id)))
    completions_all = sorted(
        (item for item in data.completions if item.completed_at <= as_of and _matches(
            filters, item.category, item.tags, item.planned_weekday or day_of_week_for_date(item.local_date),
            item.planned_bucket)),
        key=lambda item: (item.completed_at, item.execution_id))

    range_start = today - timedelta(days=range_days - 1) if range_days is not None else None
    planned = [item for item in planned_all
               if (range_start is None or item.local_date >= range_start) and item.local_date <= today]
    completions = [item for item in completions_all if range_start is None or item.local_date >= range_start]

    activity_dates = [item.local_date for item in planned] + [item.local_date for item in completions]
    first_activity = min(activity_dates) if activity_dates else None

    planned_by_date: dict[date, list[PlannedItem]] = defaultdict(list)
    for item in planned_all:
        planned_by_date[item.local_date].append(item)
    completions_by_date: dict[date, list[CompletionItem]] = defaultdict(list)
    for item in completions:
        completions_by_date[item.local_date].append(item)

    labels = _TypeLabels(data, planned_all, completions_all)
    unknown_points = sum(1 for item in completions if item.points is None)
    completeness = _completeness(data, planned, completions, unknown_points, range_start, today)
    point_qualifications = _point_qualifications(data, unknown_points)

    classes = {day: _day_class(items) for day, items in planned_by_date.items()}
    general = GeneralSection(
        counts=_counts(planned, thresholds), activity=_activity(completions), durations=_durations(planned),
        median_start_delay_minutes=_median([i.start_delay_minutes for i in planned if i.start_delay_minutes is not None]),
        start_delay_samples=sum(1 for item in planned if item.start_delay_minutes is not None),
        highest_point_day=_highest_point_day(completions_by_date, first_activity, today, point_qualifications),
        best_week=_best_week(completions_by_date, first_activity, today, point_qualifications),
        most_completed_type=_most_completed_type(planned, completions, labels, thresholds, data),
        **_streak_awards(classes, planned_by_date, first_activity, range_start, today, data),
        averages=_averages(planned_by_date, completions_by_date, first_activity, today, thresholds),
    )
    return TrackerReport(
        timezone=timezone_name, as_of=as_of, today=today, range_days=range_days, range_start=range_start,
        range_end=today, filters=filters, completeness=completeness, general=general,
        types=_types(planned_all, completions_all, labels, today, thresholds),
        time=_time_section(data, planned, completions, planned_by_date, completions_by_date, planned_all,
                           completions_all, classes, labels, first_activity, range_start, today, thresholds),
        records=_records(planned_all, completions_all),
        type_labels={str(type_id): labels(type_id) for type_id in
                     {item.type_id for item in (*planned_all, *completions_all) if item.type_id is not None}},
        categories=sorted({item.category for item in (*data.planned, *data.completions) if item.category}),
        tags=sorted({tag for item in (*data.planned, *data.completions) for tag in item.tags or ()}),
    )


def _records(planned: list[PlannedItem], completions: list[CompletionItem]) -> dict[str, RecordRef]:
    records = {
        f"p:{item.placement_id}": RecordRef(
            kind="occurrence", id=str(item.placement_id), name=item.name, category=item.category,
            type_id=item.type_id, local_date=item.local_date, state=item.state.value, due=item.due,
            planned_start=item.planned_start, active_minutes=item.actual_active_minutes)
        for item in planned
    }
    records.update({
        f"e:{item.execution_id}": RecordRef(
            kind="completion", id=item.execution_id, name=item.name, category=item.category, type_id=item.type_id,
            local_date=item.local_date, completed_at=item.completed_at, points=item.points,
            active_minutes=item.active_minutes)
        for item in completions
    })
    return records


class _TypeLabels:
    """One display label per type: its current record's label, else the newest label any record carries."""

    def __init__(self, data: TrackerData, planned: list[PlannedItem], completions: list[CompletionItem]) -> None:
        self._labels = dict(data.type_labels)
        for item in planned:  # oldest first: a later name wins only where no type record exists
            if item.type_id is not None and item.type_id not in data.type_labels and item.name:
                self._labels[item.type_id] = item.name
        for item in completions:
            if item.type_id is not None and item.type_id not in self._labels and item.name:
                self._labels[item.type_id] = item.name

    def __call__(self, type_id: uuid.UUID | None) -> str:
        if type_id is None:
            return "Unknown type"
        return self._labels.get(type_id, f"Type {str(type_id)[:8]}")


def _point_qualifications(data: TrackerData, unknown_points: int) -> list[str]:
    notes = []
    if unknown_points:
        notes.append(f"{unknown_points} completion(s) in the range have no recorded points and are not counted")
    if data.unknown_completion_dates:
        notes.append(f"{data.unknown_completion_dates} completion(s) have no recorded completion time and are not dated")
    if data.history_truncated:
        notes.append("history older than the reading limit was not read")
    if data.lineage_truncated:
        notes.append("a chain of moves was longer than the lineage limit; older plans are missing")
    return notes


def _completeness(data: TrackerData, planned, completions, unknown_points: int, range_start, today) -> Completeness:
    def in_range(day: date) -> bool:
        return (range_start is None or day >= range_start) and day <= today

    notes = _point_qualifications(data, unknown_points)
    unknown_occurrences = sum(1 for item in planned if item.type_id is None)
    unknown_completions = sum(1 for item in completions if item.type_id is None)
    if unknown_occurrences or unknown_completions:
        notes.append(f"{unknown_occurrences} planned occurrence(s) and {unknown_completions} completion(s) have no "
                     "known task type and are grouped as unknown")
    if data.duplicate_completions:
        notes.append(f"{data.duplicate_completions} earlier completion(s) of an occurrence that was completed again "
                     "are not counted twice")
    return Completeness(
        complete=not (data.history_truncated or data.lineage_truncated or data.unknown_completion_dates
                      or unknown_points),
        notes=notes, history_truncated=data.history_truncated, lineage_truncated=data.lineage_truncated,
        unknown_completion_dates=data.unknown_completion_dates, unknown_points=unknown_points,
        unknown_type_occurrences=unknown_occurrences, unknown_type_completions=unknown_completions,
        duplicate_completions=data.duplicate_completions,
        removed_from_plan=sum(count for day, count in data.removed_by_date.items() if in_range(day)),
        moved_out=sum(count for day, count in data.moved_out_by_date.items() if in_range(day)),
        windows_read=data.windows_read,
    )


# -- awards ---------------------------------------------------------------------


def _point_winner(key: str, label: str, start: date, end: date, items: list[CompletionItem]) -> AwardWinner:
    totals = _activity(items)
    return AwardWinner(
        key=key, label=label, start_date=start, end_date=end, value=float(totals.known_points),
        detail={"completions": totals.completions, "known_point_completions": totals.known_point_completions,
                "unknown_point_completions": totals.unknown_point_completions,
                "productive_minutes": totals.productive_minutes},
        execution_ids=[item.execution_id for item in items],
        placement_ids=[item.placement_id for item in items if item.placement_id is not None],
    )


def _ranked_award(kind, title, basis, calculation, unit, candidates: list[AwardWinner], qualifications, empty,
                  partial: AwardWinner | None = None) -> Award:
    if not candidates:
        return Award(kind=kind, title=title, available=False, unavailable_reason=empty, basis=basis,
                     calculation=calculation, unit=unit, partial=partial, qualified=bool(qualifications),
                     qualifications=list(qualifications))
    best = max(candidate.value for candidate in candidates)
    winners = [candidate for candidate in candidates if candidate.value == best]
    return Award(kind=kind, title=title, available=True, basis=basis, calculation=calculation, unit=unit, value=best,
                 winners=winners, tie_count=len(winners), sample_count=len(candidates), partial=partial,
                 qualified=bool(qualifications), qualifications=list(qualifications))


def _highest_point_day(by_date, first_activity, today: date, qualifications) -> Award:
    candidates = [
        _point_winner(day.isoformat(), day.isoformat(), day, day, items)
        for day, items in sorted(by_date.items())
        if day < today and any(item.points is not None for item in items)
    ]
    partial = _point_winner(today.isoformat(), today.isoformat(), today, today, by_date.get(today, []))
    return _ranked_award(
        "highest_point_day", "Highest-point day", COMPLETION_BASIS,
        "the sum of the points snapshots of the work completed on each finished local date; today is shown "
        "separately and cannot be the record", "points", candidates, qualifications,
        "no finished day has a completion with recorded points", partial)


def _week_items(by_date, start: date) -> list[CompletionItem]:
    return [item for day in _days(start, start + timedelta(days=6)) for item in by_date.get(day, [])]


def _complete_weeks(first_activity: date | None, today: date) -> list[date]:
    """The Mondays of the Monday-Sunday weeks lying entirely within [first_activity, yesterday]."""
    if first_activity is None:
        return []
    start = week_start(first_activity)
    if start < first_activity:
        start += timedelta(days=7)
    weeks = []
    while start + timedelta(days=6) < today:
        weeks.append(start)
        start += timedelta(days=7)
    return weeks


def _best_week(by_date, first_activity, today: date, qualifications) -> Award:
    def winner(start: date, items) -> AwardWinner:
        end = start + timedelta(days=6)
        return _point_winner(start.isoformat(), f"{start.isoformat()} to {end.isoformat()}", start, end, items)

    candidates = []
    for start in _complete_weeks(first_activity, today):
        items = _week_items(by_date, start)
        if any(item.points is not None for item in items):
            candidates.append(winner(start, items))
    current = week_start(today)
    partial = winner(current, [item for day in _days(current, today) for item in by_date.get(day, [])])
    return _ranked_award(
        "best_week", "Best completed week", COMPLETION_BASIS,
        "the sum of the points snapshots of the work completed in each whole Monday-Sunday week that has ended; "
        "the current week is shown separately", "points", candidates, qualifications,
        "no whole Monday-Sunday week with a completion that has recorded points has ended", partial)


def _most_completed_type(planned, completions, labels: _TypeLabels, thresholds, data: TrackerData) -> Award:
    by_type: dict[uuid.UUID, list[CompletionItem]] = defaultdict(list)
    for item in completions:
        if item.type_id is not None:
            by_type[item.type_id].append(item)
    planned_by_type: dict[uuid.UUID, list[PlannedItem]] = defaultdict(list)
    for item in planned:
        if item.type_id is not None:
            planned_by_type[item.type_id].append(item)
    candidates = []
    for type_id, items in sorted(by_type.items(), key=lambda pair: (labels(pair[0]).casefold(), str(pair[0]))):
        rate = _counts(planned_by_type.get(type_id, []), thresholds).due_completion
        candidates.append(AwardWinner(
            key=str(type_id), label=labels(type_id), value=float(len(items)),
            detail={"completions": len(items), "due_completion_rate": rate.value, "due_completed": rate.numerator,
                    "due_denominator": rate.denominator},
            execution_ids=[item.execution_id for item in items],
            placement_ids=[item.placement_id for item in items if item.placement_id is not None],
        ))
    qualifications = []
    unknown = sum(1 for item in completions if item.type_id is None)
    if unknown:
        qualifications.append(f"{unknown} completion(s) have no known task type and are not ranked")
    if data.history_truncated:
        qualifications.append("history older than the reading limit was not read")
    return _ranked_award(
        "most_completed_type", "Most completed task type", COMPLETION_BASIS,
        "the number of completions of each task type in the range; its due-completion rate (planned-date basis) is "
        "shown beside it and does not decide the ranking", "completions", candidates, qualifications,
        "no completion with a known task type")


def _streak_winner(streak: Streak, planned_by_date) -> AwardWinner:
    items = [item for day in _days(streak.start, streak.end) for item in planned_by_date.get(day, [])]
    return AwardWinner(
        key=streak.start.isoformat(), label=f"{streak.start.isoformat()} to {streak.end.isoformat()}",
        start_date=streak.start, end_date=streak.end, value=float(streak.length),
        detail={"days": streak.length, "scheduled": len(items),
                "completed": sum(1 for item in items if item.state == OccurrenceState.COMPLETED)},
        placement_ids=[item.placement_id for item in items],
        execution_ids=[item.execution_id for item in items if item.execution_id is not None],
    )


def _streak_awards(classes, planned_by_date, first_activity, range_start, today: date, data: TrackerData) -> dict:
    basis = PLANNED_BASIS
    rule = ("consecutive local dates whose scheduled occurrences are at least 60% completed (the calendar's green "
            "days; a cancelled attempt counts as uncompleted). An empty day or a finished non-green day ends a streak")
    qualifications = []
    if data.history_truncated:
        qualifications.append("history older than the reading limit was not read")
    if data.lineage_truncated:
        qualifications.append("a chain of moves was longer than the lineage limit; older plans are missing")
    empty = dict(basis=basis, unit="days", qualified=bool(qualifications), qualifications=qualifications)
    if first_activity is None:
        missing = "no scheduled work in the range"
        return {
            "longest_green_streak": Award(kind="longest_green_streak", title="Longest green-day streak",
                                          available=False, unavailable_reason=missing, calculation=rule, **empty),
            "current_green_streak": Award(kind="current_green_streak", title="Current green-day streak",
                                          available=False, unavailable_reason=missing, calculation=rule, **empty),
        }
    # The longest streak is searched in the selected range; the current one always reaches back as far as it goes.
    all_dates = [day for day in classes if classes[day][0] != DayStatusClass.NO_TASKS]
    history_start = min(all_dates) if all_dates else first_activity
    runs, current, provisional = green_streaks(classes, min(history_start, first_activity), today)
    in_range = [
        Streak(max(run.start, range_start) if range_start else run.start, run.end)
        for run in runs if range_start is None or run.end >= range_start
    ]
    longest = _ranked_award(
        "longest_green_streak", "Longest green-day streak", basis, rule, "days",
        [_streak_winner(run, planned_by_date) for run in in_range], qualifications, "no green day in the range")
    note = " Today is not finished, so it neither extends nor ends the streak." if provisional else ""
    current_award = Award(
        kind="current_green_streak", title="Current green-day streak", available=True, calculation=rule + "." + note,
        value=float(current.length) if current else 0.0,
        winners=[_streak_winner(current, planned_by_date)] if current else [], tie_count=1 if current else 0,
        sample_count=len(runs), **empty,
    )
    return {"longest_green_streak": longest, "current_green_streak": current_award}


# -- averages ---------------------------------------------------------------------


def _averages(planned_by_date, completions_by_date, first_activity, today: date, thresholds) -> Averages:
    yesterday = today - timedelta(days=1)
    today_totals = _activity(completions_by_date.get(today, []))
    current_week = _activity([item for day in _days(week_start(today), today)
                              for item in completions_by_date.get(day, [])])
    empty_rate = _rate(0, 0, thresholds, "no due, non-cancelled occurrences")
    if first_activity is None or first_activity > yesterday:
        reason = "no recorded activity" if first_activity is None else "no finished day in the range yet"
        return Averages(available=False, unavailable_reason=reason, first_activity_date=first_activity,
                        pooled_due_completion=empty_rate, today=today_totals, current_week=current_week)

    elapsed = list(_days(first_activity, yesterday))
    totals = _activity([item for day in elapsed for item in completions_by_date.get(day, [])])
    daily = AverageSet(periods=len(elapsed), completed_tasks=round(totals.completions / len(elapsed), 2),
                       points=round(totals.known_points / len(elapsed), 2),
                       productive_minutes=round(totals.productive_minutes / len(elapsed), 2))
    weeks = _complete_weeks(first_activity, today)
    weekly = None
    if weeks:
        in_weeks = _activity([item for start in weeks for item in _week_items(completions_by_date, start)])
        weekly = AverageSet(periods=len(weeks), completed_tasks=round(in_weeks.completions / len(weeks), 2),
                            points=round(in_weeks.known_points / len(weeks), 2),
                            productive_minutes=round(in_weeks.productive_minutes / len(weeks), 2))

    rates, pooled_items = [], []
    for day in elapsed:
        items = planned_by_date.get(day, [])
        counts = _counts(items, thresholds)
        if counts.due_denominator:
            rates.append(counts.due_completed / counts.due_denominator)
            pooled_items.extend(items)
    pooled = _counts(pooled_items, thresholds).due_completion
    return Averages(
        available=True, first_activity_date=first_activity, through_date=yesterday, elapsed_days=len(elapsed),
        complete_weeks=len(weeks), daily=daily, weekly=weekly, totals=totals,
        average_daily_due_completion=round(statistics.mean(rates), 4) if rates else None, due_days=len(rates),
        pooled_due_completion=pooled, today=today_totals, current_week=current_week,
    )


# -- per-type views -----------------------------------------------------------------


def _best_weekday(items: list[PlannedItem], thresholds: ProductivityThresholds) -> RankedGroup:
    basis = "due-completion rate by planned weekday, among weekdays with enough due work to compare"
    rates = {}
    for weekday in WEEKDAYS:
        counts = _counts([i for i in items if day_of_week_for_date(i.local_date) == weekday], thresholds)
        if counts.due_denominator >= thresholds.low:
            rates[weekday] = counts
    if not rates:
        return RankedGroup(available=False, basis=basis,
                           unavailable_reason=f"no weekday has at least {thresholds.low} due occurrences")
    best = max(counts.due_completion.value for counts in rates.values())
    winners = [weekday for weekday in WEEKDAYS if weekday in rates and rates[weekday].due_completion.value == best]
    first = rates[winners[0]]
    return RankedGroup(available=True, basis=basis, winners=winners, value=best, numerator=first.due_completed,
                       denominator=first.due_denominator,
                       detail={weekday: counts.due_completion.value for weekday, counts in rates.items()})


def _supported_slot(key: str, label: str, items: list[PlannedItem], thresholds) -> SupportedSlot | None:
    samples: dict[str, int] = defaultdict(int)
    for item in items:
        if item.state == OccurrenceState.COMPLETED and item.actual_active_minutes is not None:
            samples[item.bucket] += 1
    if not samples:
        return None
    best = max(samples.values())
    level = evidence_level_for_count(best, thresholds)
    return SupportedSlot(
        key=key, label=label, buckets=[bucket for bucket in BUCKETS if samples.get(bucket) == best],
        sample_count=best, evidence_level=level,
        note="the planned-start slot with the most timed completions -- the most evidence, not the best "
             "performance" + ("; too few samples to rely on" if level in (EvidenceLevel.INSUFFICIENT,
                                                                           EvidenceLevel.LOW) else ""),
    )


def _types(planned_all, completions_all, labels: _TypeLabels, today: date, thresholds) -> list[TypeView]:
    periods = {
        "today": (today, today),
        "week": (week_start(today), week_start(today) + timedelta(days=6)),
        "month": (today.replace(day=1), month_end(today)),
        "all_time": (None, today),
    }
    planned_by_type: dict[uuid.UUID | None, list[PlannedItem]] = defaultdict(list)
    for item in planned_all:
        planned_by_type[item.type_id].append(item)
    completions_by_type: dict[uuid.UUID | None, list[CompletionItem]] = defaultdict(list)
    for item in completions_all:
        completions_by_type[item.type_id].append(item)

    views = []
    for type_id in set(planned_by_type) | set(completions_by_type):
        planned, completions = planned_by_type.get(type_id, []), completions_by_type.get(type_id, [])
        type_periods = {}
        for name, (start, end) in periods.items():
            in_period = [i for i in planned if (start is None or i.local_date >= start) and i.local_date <= end]
            done = [i for i in completions if (start is None or i.local_date >= start) and i.local_date <= end]
            activity = _activity(done)
            type_periods[name] = TypePeriod(
                period=name, start_date=start, end_date=end, counts=_counts(in_period, thresholds),
                activity=activity, durations=_durations(in_period), productive_minutes=activity.productive_minutes)
        past = [item for item in planned if item.local_date <= today]
        views.append(TypeView(
            type_id=type_id, label=labels(type_id), periods=type_periods,
            best_weekday=_best_weekday(past, thresholds),
            supported_slot=_supported_slot(str(type_id), labels(type_id), past, thresholds),
            placement_ids=[item.placement_id for item in planned],
            execution_ids=[item.execution_id for item in completions],
        ))
    views.sort(key=lambda view: (view.type_id is None, view.label.casefold(), str(view.type_id)))
    return views


# -- time views -----------------------------------------------------------------------


def _period_views(days: list[DayView], key_of: Callable[[date], tuple[str, date, date]], planned_by_date,
                  completions_by_date, today: date, thresholds) -> list[PeriodView]:
    groups: dict[str, tuple[date, date, list[DayView]]] = {}
    for day in days:
        key, start, end = key_of(day.local_date)
        groups.setdefault(key, (start, end, []))[2].append(day)
    views = []
    for key, (start, end, members) in groups.items():
        dates = [member.local_date for member in members]
        views.append(PeriodView(
            key=key, start_date=start, end_date=end,
            complete=len(members) == (end - start).days + 1 and end < today, days_in_range=len(members),
            counts=_counts([i for day in dates for i in planned_by_date.get(day, [])], thresholds),
            activity=_activity([i for day in dates for i in completions_by_date.get(day, [])]),
            green_days=sum(1 for member in members if member.status_class in GREEN_CLASSES),
        ))
    return sorted(views, key=lambda view: view.start_date)


def _time_section(data: TrackerData, planned, completions, planned_by_date, completions_by_date, planned_all,
                  completions_all, classes, labels: _TypeLabels, first_activity, range_start, today: date,
                  thresholds) -> TimeSection:
    start = range_start if range_start is not None else first_activity
    if range_start is None:
        # A date whose only history is a plan that was moved away or removed is still listed, with that note.
        noted = [day for day in (*data.removed_by_date, *data.moved_out_by_date) if day <= today]
        if noted:
            start = min([*noted, *([start] if start is not None else [])])
    days: list[DayView] = []
    for day in (_days(start, today) if start is not None else ()):
        items, done = planned_by_date.get(day, []), completions_by_date.get(day, [])
        days.append(DayView(
            local_date=day, weekday=day_of_week_for_date(day), partial=day >= today,
            counts=_counts(items, thresholds), status_class=classes.get(day, (DayStatusClass.NO_TASKS, 0))[0],
            activity=_activity(done), removed=data.removed_by_date.get(day, 0),
            moved_out=data.moved_out_by_date.get(day, 0), placement_ids=[item.placement_id for item in items],
            execution_ids=[item.execution_id for item in done],
        ))

    def week_key(day: date) -> tuple[str, date, date]:
        monday = week_start(day)
        return monday.isoformat(), monday, monday + timedelta(days=6)

    def month_key(day: date) -> tuple[str, date, date]:
        return f"{day.year:04d}-{day.month:02d}", day.replace(day=1), month_end(day)

    yesterday = today - timedelta(days=1)
    eligible = defaultdict(int)
    if first_activity is not None:
        for day in _days(first_activity, yesterday):
            eligible[day_of_week_for_date(day)] += 1
    weekdays = []
    for weekday in WEEKDAYS:
        done = [i for i in completions if day_of_week_for_date(i.local_date) == weekday and i.local_date <= yesterday]
        activity = _activity(done)
        weekdays.append(WeekdayView(
            weekday=weekday, counts=_counts([i for i in planned if day_of_week_for_date(i.local_date) == weekday],
                                            thresholds),
            activity=activity, eligible_days=eligible[weekday],
            average_points_per_eligible_day=round(activity.known_points / eligible[weekday], 2)
            if eligible[weekday] else None,
        ))

    points_basis = ("average known points per elapsed calendar date of the weekday, from the first activity through "
                    "yesterday (dates without points count as zero)")
    ranked = [view for view in weekdays if view.average_points_per_eligible_day is not None
              and view.activity.known_point_completions]
    if ranked:
        best = max(view.average_points_per_eligible_day for view in ranked)
        winners = [view for view in ranked if view.average_points_per_eligible_day == best]
        highest_points = RankedGroup(
            available=True, basis=points_basis, winners=[view.weekday for view in winners], value=best,
            numerator=float(winners[0].activity.known_points), denominator=float(winners[0].eligible_days),
            detail={view.weekday: view.activity.known_points for view in weekdays})
    else:
        highest_points = RankedGroup(available=False, basis=points_basis,
                                     unavailable_reason="no completion with recorded points on a finished day")

    categories = sorted({item.category for item in planned if item.category is not None})
    type_ids = sorted({item.type_id for item in planned if item.type_id is not None},
                      key=lambda type_id: (labels(type_id).casefold(), str(type_id)))
    recent_start = today - timedelta(days=6)
    return TimeSection(
        days=days,
        weeks=_period_views(days, week_key, planned_by_date, completions_by_date, today, thresholds),
        months=_period_views(days, month_key, planned_by_date, completions_by_date, today, thresholds),
        weekdays=weekdays,
        buckets=[BucketView(bucket=bucket, counts=_counts([i for i in planned if i.bucket == bucket], thresholds))
                 for bucket in BUCKETS],
        highest_completion_weekday=_best_weekday(planned, thresholds),
        highest_points_weekday=highest_points,
        supported_slots_by_category=[slot for category in categories if (slot := _supported_slot(
            category, category, [i for i in planned if i.category == category], thresholds)) is not None],
        supported_slots_by_type=[slot for type_id in type_ids if (slot := _supported_slot(
            str(type_id), labels(type_id), [i for i in planned if i.type_id == type_id], thresholds)) is not None],
        planned_vs_actual_by_category=[
            PlannedVsActual(key=category, label=category,
                            durations=_durations([i for i in planned if i.category == category]))
            for category in categories],
        planned_vs_actual_by_type=[
            PlannedVsActual(key=str(type_id), label=labels(type_id),
                            durations=_durations([i for i in planned if i.type_id == type_id]))
            for type_id in type_ids],
        recent=RecentComparison(
            recent=PeriodTotals(
                start_date=recent_start, end_date=today,
                counts=_counts([i for i in planned_all if recent_start <= i.local_date <= today], thresholds),
                activity=_activity([i for i in completions_all if recent_start <= i.local_date <= today])),
            baseline=PeriodTotals(start_date=start or today, end_date=today, counts=_counts(planned, thresholds),
                                  activity=_activity(completions)),
        ),
    )


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------


class TrackerSource(Protocol):
    """An owner-scoped reader of planning history (a PlanningService or a planning repository)."""

    def schedule_history(self, start_utc: datetime, end_utc: datetime) -> ScheduleHistory: ...

    def completion_history(self, start_utc: datetime, end_utc: datetime) -> CompletionHistory: ...

    def history_bounds(self) -> HistoryBounds: ...


def planned_items(history: ScheduleHistory, report: ScheduleCohortReport) -> list[PlannedItem]:
    """The report's occurrences with their historical name, tags and type (app.planning.history.historical_plan)."""
    items = []
    for occurrence in report.occurrences:
        placement = history.placements[occurrence.placement_id]
        linked = history.executions.get(occurrence.placement_id)
        plan = historical_plan(placement, linked.execution if linked else None, history.tasks.get(occurrence.task_id))
        estimate = occurrence.estimate_minutes if occurrence.estimate_minutes is not None else (
            float(plan.estimate_minutes) if plan.estimate_minutes is not None else None)
        delay = None
        if occurrence.actual_first_start is not None:
            delay = round((occurrence.actual_first_start - occurrence.planned_start).total_seconds() / 60, 2)
        items.append(PlannedItem(
            placement_id=occurrence.placement_id, task_id=occurrence.task_id, local_date=occurrence.local_date,
            planned_start=occurrence.planned_start,
            bucket=time_bucket_for_instant(occurrence.planned_start, occurrence.plan_timezone).value,
            planned_minutes=occurrence.planned_minutes, due=occurrence.due, state=occurrence.state,
            execution_id=occurrence.execution_id, name=plan.name, category=occurrence.category, tags=plan.tags,
            type_id=plan.type_id, estimate_minutes=estimate, actual_active_minutes=occurrence.actual_active_minutes,
            start_delay_minutes=delay,
        ))
    return items


def completion_items(history: CompletionHistory, timezone_name: str) -> list[CompletionItem]:
    """
    The history's completed executions as completion-date records. Each
    carries the key of its occurrence -- the last placement of its lineage,
    or the execution itself when it has no placement -- so an occurrence
    completed under two placements is counted once (merge_completions).
    """
    items = []
    for execution in history.executions:
        placement = history.placements.get(execution.scheduled_task_id) if execution.scheduled_task_id else None
        task = history.tasks.get(execution.task_id) if execution.task_id else None
        key, weekday, bucket = f"execution:{execution.id}", None, None
        if placement is not None:
            plan = historical_plan(placement, execution, task)
            last, seen = placement, {placement.id}
            while last.superseded_by_id in history.placements and last.superseded_by_id not in seen:
                last = history.placements[last.superseded_by_id]
                seen.add(last.id)
            key = f"placement:{last.id}"
            name, category, tags, type_id = plan.name, plan.category, plan.tags, plan.type_id
            weekday = day_of_week_for_date(local_date_of(placement.planned_start, timezone_name))
            bucket = time_bucket_for_instant(placement.planned_start, placement.timezone).value
        else:
            name, category = execution.task_name, execution.category
            tags = (execution.tag,) if execution.tag else None
            type_id = task.task_type_id if task is not None else None
        items.append(CompletionItem(
            execution_id=execution.id, occurrence_key=key, task_id=execution.task_id,
            placement_id=execution.scheduled_task_id, completed_at=utc(execution.actual_final_end_at),
            local_date=local_date_of(execution.actual_final_end_at, timezone_name), points=execution.points,
            active_minutes=execution.actual_active_duration_minutes, name=name, category=category, tags=tags,
            type_id=type_id, planned_weekday=weekday, planned_bucket=bucket,
        ))
    return items


def merge_completions(items: Iterable[CompletionItem]) -> tuple[list[CompletionItem], int]:
    """(one completion per occurrence -- its latest, how many earlier ones were dropped), deterministic."""
    latest: dict[str, CompletionItem] = {}
    seen: set[str] = set()
    dropped = 0
    for item in sorted(items, key=lambda item: (item.completed_at, item.execution_id)):
        if item.execution_id in seen:
            continue  # the same execution read by two windows
        seen.add(item.execution_id)
        if item.occurrence_key in latest:
            dropped += 1
        latest[item.occurrence_key] = item
    # A task completed on a time slot is that completion: a completion of the same task recorded without a
    # placement (from its project) is the same work, never a second one.
    scheduled_tasks = {item.task_id for item in latest.values() if item.placement_id is not None and item.task_id}
    direct = [key for key, item in latest.items() if item.placement_id is None and item.task_id in scheduled_tasks]
    for key in direct:
        del latest[key]
    dropped += len(direct)
    return sorted(latest.values(), key=lambda item: (item.completed_at, item.execution_id)), dropped


def read_tracker_data(
    source: TrackerSource,
    *,
    timezone_name: str,
    as_of: datetime,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
    max_history_days: int = MAX_HISTORY_DAYS,
) -> TrackerData:
    """
    Read the whole recorded history up to the end of the current week and
    month (so the per-type periods see their planned future work) in windows
    of at most MAX_REPORT_DAYS: three reads for the bounds and two per window.
    """
    validate_timezone(timezone_name)
    as_of = utc(as_of)
    today = local_date_of(as_of, timezone_name)
    data = TrackerData()
    bounds = source.history_bounds()
    firsts = [local_date_of(instant, timezone_name)
              for instant in (bounds.first_planned_start, bounds.first_completion) if instant is not None]
    if not firsts:
        return data
    first = min(firsts)
    oldest_read = today - timedelta(days=max_history_days - 1)
    if first < oldest_read:
        first, data.history_truncated = oldest_read, True
    last = max(month_end(today), week_start(today) + timedelta(days=6))

    planned: dict[uuid.UUID, PlannedItem] = {}
    completions: list[CompletionItem] = []
    removed: dict[date, int] = defaultdict(int)
    moved: dict[date, int] = defaultdict(int)
    start = first
    while start <= last:
        end = min(start + timedelta(days=MAX_REPORT_DAYS - 1), last)
        window = report_window(start, end, timezone_name)
        history = source.schedule_history(window.start_utc, window.end_utc)
        report = build_schedule_cohort_report(history, window, as_of=as_of, thresholds=thresholds)
        for item in planned_items(history, report):
            planned[item.placement_id] = item
        _removals(history, window, as_of, timezone_name, removed, moved)
        done = source.completion_history(window.start_utc, window.end_utc)
        completions.extend(completion_items(done, timezone_name))
        for record in (*history.task_types.values(), *done.task_types.values()):
            data.type_labels[record.id] = record.label
        data.lineage_truncated = data.lineage_truncated or history.lineage_truncated or done.lineage_truncated
        data.unknown_completion_dates = done.unknown_completion_dates
        data.windows_read += 1
        start = end + timedelta(days=1)

    data.planned = list(planned.values())
    data.completions, data.duplicate_completions = merge_completions(completions)
    data.removed_by_date, data.moved_out_by_date = dict(removed), dict(moved)
    return data


def _removals(history: ScheduleHistory, window, as_of: datetime, timezone_name: str, removed, moved) -> None:
    """Per local date of this window: tombstones removed without a successor, and ones moved to another date."""
    for placement in history.placements.values():
        if not window.start_utc <= utc(placement.planned_start) < window.end_utc:
            continue
        if placement.deleted_at is None or placement.deleted_at > as_of:
            continue
        day = local_date_of(placement.planned_start, timezone_name)
        if placement.superseded_by_id is None:
            removed[day] += 1
            continue
        successor = history.placements.get(placement.superseded_by_id)
        if successor is None or local_date_of(successor.planned_start, timezone_name) != day:
            moved[day] += 1
