"""
reporting.py

The productivity-analysis service boundary. ProductivityService is the one
class the CLI (app/productivity/report_cli.py) and any future UI or
analytics code should call -- it is the only place that wires the
repository, data prep, segmenting, insight generation, and prediction
together. Nothing here executes SQL directly (that stays inside
app/execution/repository.py).

Two views, deliberately separate (docs/analytics.md):
    - the terminal-outcome statistics (generate_report/build_dashboard,
      unchanged): executions filtered by created_at; completion among
      resolved executions = completed / (completed + skipped + cancelled);
    - the schedule cohort (build_schedule_cohort_report, Milestone 5): the
      intended occurrences planned in a local date range -- placements with
      or without an execution -- as of an explicit cutoff, in an explicit
      reporting timezone (app/productivity/schedule_cohort.py).

The tracker (build_tracker_report, app/productivity/tracker.py) adds the
awards, averages, per-type and time views. It keeps the bases apart: counts
and due rates by planned date, earned points and completed activity by
completion date; the execution-created statistics above are not part of it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone

from pydantic import BaseModel

from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket, day_of_week_for_timestamp, time_bucket_for_minutes
from app.productivity.data_prep import Observation, Period, build_observations
from app.productivity.filters import ObservationFilters, apply_filters
from app.productivity.insights import Insight, generate_insights
from app.productivity.ml_prediction import MLDurationPrediction, predict_duration_with_ml
from app.productivity.prediction import DurationPrediction, predict_duration
from app.planning.history import ScheduleHistory
from app.productivity.schedule_cohort import (
    ScheduleCohortReport,
    ScheduleHistorySource,
    read_schedule_cohort_report,
    read_schedule_history_and_report,
)
from app.productivity.segments import (
    best_supported_time_bucket_by_category,
    by_category,
    by_category_and_time_bucket,
    by_day_of_week,
    by_tag,
    by_time_bucket,
    global_stats,
)
from app.productivity.stats import ProductivityThresholds, SegmentStats
from app.productivity.tracker import TrackerFilters, TrackerReport, build_tracker_report, read_tracker_data
from app.productivity.trends import RecentTrend, compute_recent_trend

_RECENT_TREND_WINDOW_DAYS = 7


class DurationPredictionComparison(BaseModel):
    """
    Both predictors' output for the same task, side by side, purely for
    comparison. This never changes which predictor is used in production --
    predict_duration (below) remains the median-predictor-only production
    path, untouched. ml_prediction is None whenever the evidence-gated ML
    model isn't currently active or couldn't produce a result; see
    app/productivity/ml_prediction.py for the fallback rules.
    """

    median_prediction: DurationPrediction
    ml_prediction: MLDurationPrediction | None


class ProductivityReport(BaseModel):
    """A complete, deterministic snapshot of the productivity analysis for one period."""

    generated_at: str
    period: Period
    observation_count: int

    global_stats: SegmentStats
    by_category: dict[str, SegmentStats]
    by_tag: dict[str, SegmentStats]
    by_day_of_week: dict[str, SegmentStats]
    by_time_bucket: dict[str, SegmentStats]
    by_category_and_time_bucket: dict[str, SegmentStats]

    insights: list[Insight]


class ProductivityDashboard(BaseModel):
    """
    A UI-shaped bundle of everything a productivity dashboard/page needs from
    one call, built from the same filtered observation set: summary stats,
    category/time-bucket breakdowns, each category's best-supported time
    bucket, a recent-vs-baseline trend, and insights.
    """

    generated_at: str
    window_days: int | None
    observation_count: int

    global_stats: SegmentStats
    by_category: dict[str, SegmentStats]
    by_tag: dict[str, SegmentStats]
    by_day_of_week: dict[str, SegmentStats]
    by_time_bucket: dict[str, SegmentStats]
    by_category_and_time_bucket: dict[str, SegmentStats]
    best_supported_time_bucket_by_category: dict[str, str]

    recent_trend: RecentTrend
    insights: list[Insight]


class ProductivityService:
    """
    Builds ProductivityReport and DurationPrediction results from execution
    history, and -- given a schedule-history source (the owner-scoped
    PlanningService) and a reporting timezone -- the schedule-cohort report.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        thresholds: ProductivityThresholds = ProductivityThresholds(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        *,
        history: ScheduleHistorySource | None = None,
        timezone_name: str | None = None,
    ) -> None:
        self._repository = repository
        self._thresholds = thresholds
        self._clock = clock
        self._history = history
        self._timezone = timezone_name

    @property
    def reporting_timezone(self) -> str | None:
        return self._timezone

    def build_schedule_cohort_report(
        self,
        *,
        start_date: date,
        end_date: date,
        timezone_name: str | None = None,
        as_of: datetime | None = None,
    ) -> ScheduleCohortReport:
        """
        The schedule-cohort report of [start_date, end_date] (local dates in
        `timezone_name`, default: this service's reporting timezone -- never
        the host's) as of `as_of` (default: now). Read-only: it never creates
        an execution or changes a record.
        """
        if self._history is None:
            raise ValueError("this productivity service has no schedule history source.")
        tz = timezone_name or self._timezone
        if tz is None:
            raise ValueError("a reporting timezone is required (the host timezone is never assumed).")
        now = self._clock()
        return read_schedule_cohort_report(
            self._history, start_date=start_date, end_date=end_date, timezone_name=tz,
            as_of=as_of or now, now=now, thresholds=self._thresholds,
        )

    def build_tracker_report(
        self,
        *,
        range_days: int | None = None,
        filters: TrackerFilters | None = None,
        timezone_name: str | None = None,
        as_of: datetime | None = None,
    ) -> TrackerReport:
        """
        The tracker report (awards, averages, per-type and time views) of the
        last `range_days` local dates (None: all time) as of `as_of` (default:
        now) in the reporting timezone. The whole recorded history is read in
        bounded windows (app/productivity/tracker.py). Read-only.
        """
        if self._history is None:
            raise ValueError("this productivity service has no schedule history source.")
        tz = timezone_name or self._timezone
        if tz is None:
            raise ValueError("a reporting timezone is required (the host timezone is never assumed).")
        now = self._clock()
        cutoff = as_of or now
        if cutoff.tzinfo is None or now.tzinfo is None:
            raise ValueError("as_of and now must be aware instants")
        if cutoff > now:
            raise ValueError("as_of cannot be later than the current time.")
        data = read_tracker_data(self._history, timezone_name=tz, as_of=cutoff, thresholds=self._thresholds)
        return build_tracker_report(data, timezone_name=tz, as_of=cutoff, range_days=range_days, filters=filters,
                                    thresholds=self._thresholds)

    def generate_report(
        self,
        *,
        period: Period = "all_time",
        filters: ObservationFilters | None = None,
    ) -> ProductivityReport:
        """
        Build a full productivity report for the given period ('all_time' or
        'last_7_days'). `filters` (optional) is applied on top: its `days`,
        if set, overrides `period`'s day window, and its category/tag/
        day_of_week/time_bucket fields narrow the observation set further.
        Omitting `filters` (the default) preserves the original two-value
        `period`-only behavior exactly.
        """
        observations = self._observations_for(period=period, filters=filters)

        category_and_bucket = by_category_and_time_bucket(observations, self._thresholds)
        category_only = by_category(observations, self._thresholds)

        insights = generate_insights(
            by_category=category_only,
            by_category_and_time_bucket=category_and_bucket,
            thresholds=self._thresholds,
        )

        return ProductivityReport(
            generated_at=self._clock().isoformat(),
            period=period,
            observation_count=len(observations),
            global_stats=global_stats(observations, self._thresholds),
            by_category=category_only,
            by_tag=by_tag(observations, self._thresholds),
            by_day_of_week=by_day_of_week(observations, self._thresholds),
            by_time_bucket=by_time_bucket(observations, self._thresholds),
            by_category_and_time_bucket={
                f"{category}/{time_bucket}": stats
                for (category, time_bucket), stats in category_and_bucket.items()
            },
            insights=insights,
        )

    def schedule_history_and_report(
        self, *, start_date: date, end_date: date, timezone_name: str | None = None, as_of: datetime | None = None,
    ) -> tuple[ScheduleHistory, ScheduleCohortReport]:
        """The range's history (placements, lineage, executions with sessions, tasks) and its cohort report."""
        if self._history is None:
            raise ValueError("this productivity service has no schedule history source.")
        tz = timezone_name or self._timezone
        if tz is None:
            raise ValueError("a reporting timezone is required (the host timezone is never assumed).")
        now = self._clock()
        return read_schedule_history_and_report(
            self._history, start_date=start_date, end_date=end_date, timezone_name=tz, as_of=as_of or now, now=now,
            thresholds=self._thresholds,
        )

    def predict_duration(
        self,
        *,
        category: str,
        time_bucket: TimeBucket,
        original_estimate_minutes: float,
        period: Period = "all_time",
        filters: ObservationFilters | None = None,
    ) -> DurationPrediction:
        """Predict a task's actual duration; see app/productivity/prediction.py for the fallback hierarchy."""
        observations = self._observations_for(period=period, filters=filters)
        return predict_duration(
            observations,
            category=category,
            time_bucket=time_bucket,
            original_estimate_minutes=original_estimate_minutes,
            thresholds=self._thresholds,
        )

    def predict_duration_comparison(
        self,
        *,
        category: str,
        tag: str,
        priority: int,
        planned_start: int,
        original_estimate_minutes: float,
        period: Period = "all_time",
        filters: ObservationFilters | None = None,
        data_dir: str | None = None,
    ) -> DurationPredictionComparison:
        """
        Predict a task's actual duration with both the median predictor (the
        one actually used in production, via `predict_duration` above) and,
        only if it is currently activated, the evidence-gated ML model --
        for side-by-side comparison. Read-only: this never writes a model
        artifact, changes the activation decision, or touches execution
        history.

        `data_dir` overrides where the persisted ML model artifact is read
        from (default: config.settings.DATA_DIR); tests pass a tmp_path here
        so they never depend on, or interfere with, the real local artifact.
        """
        observations = self._observations_for(period=period, filters=filters)
        time_bucket = time_bucket_for_minutes(planned_start)

        median_prediction = predict_duration(
            observations,
            category=category,
            time_bucket=time_bucket,
            original_estimate_minutes=original_estimate_minutes,
            thresholds=self._thresholds,
        )
        ml_prediction = predict_duration_with_ml(
            observations,
            category=category,
            tag=tag,
            priority=priority,
            time_bucket=time_bucket,
            day_of_week=day_of_week_for_timestamp(self._clock().isoformat()),
            planned_duration=round(original_estimate_minutes),
            planned_start=planned_start,
            data_dir=data_dir,
        )
        return DurationPredictionComparison(median_prediction=median_prediction, ml_prediction=ml_prediction)

    def build_observations(
        self,
        *,
        period: Period = "all_time",
        filters: ObservationFilters | None = None,
    ) -> list[Observation]:
        """Expose the flattened observation list, e.g. for a caller doing its own ad hoc analysis."""
        return self._observations_for(period=period, filters=filters)

    def build_dashboard(self, *, filters: ObservationFilters | None = None) -> ProductivityDashboard:
        """
        Build the UI-shaped ProductivityDashboard bundle for the given
        filters (all-time, unfiltered, if omitted). The recent-trend panel
        always compares a fixed 7-day recent window against the filtered
        observation set as its baseline, regardless of any `days` filter, so
        "recent" keeps a stable, well-understood meaning across filter changes.
        """
        observations = self._observations_for(period="all_time", filters=filters)

        category_and_bucket = by_category_and_time_bucket(observations, self._thresholds)
        category_only = by_category(observations, self._thresholds)

        insights = generate_insights(
            by_category=category_only,
            by_category_and_time_bucket=category_and_bucket,
            thresholds=self._thresholds,
        )

        best_supported = best_supported_time_bucket_by_category(category_and_bucket)

        recent_filters = ObservationFilters(
            days=_RECENT_TREND_WINDOW_DAYS,
            category=filters.category if filters else None,
            tag=filters.tag if filters else None,
            day_of_week=filters.day_of_week if filters else None,
            time_bucket=filters.time_bucket if filters else None,
        )
        recent_observations = self._observations_for(period="all_time", filters=recent_filters)
        recent_trend = compute_recent_trend(recent_observations, observations, self._thresholds)

        return ProductivityDashboard(
            generated_at=self._clock().isoformat(),
            window_days=filters.days if filters else None,
            observation_count=len(observations),
            global_stats=global_stats(observations, self._thresholds),
            by_category=category_only,
            by_tag=by_tag(observations, self._thresholds),
            by_day_of_week=by_day_of_week(observations, self._thresholds),
            by_time_bucket=by_time_bucket(observations, self._thresholds),
            by_category_and_time_bucket={
                f"{category}/{time_bucket}": stats
                for (category, time_bucket), stats in category_and_bucket.items()
            },
            best_supported_time_bucket_by_category={
                category: time_bucket for category, (time_bucket, _stats) in best_supported.items()
            },
            recent_trend=recent_trend,
            insights=insights,
        )

    def _observations_for(
        self,
        *,
        period: Period,
        filters: ObservationFilters | None,
    ) -> list[Observation]:
        observations = build_observations(
            self._repository,
            period=period,
            days=filters.days if filters else None,
            now=self._clock(),
        )
        return apply_filters(observations, filters)
