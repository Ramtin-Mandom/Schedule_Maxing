"""
reporting.py

The productivity-analysis service boundary. ProductivityService is the one
class the CLI (app/productivity/report_cli.py) and any future UI or
analytics code should call -- it is the only place that wires the
repository, data prep, segmenting, insight generation, and prediction
together. Nothing here executes SQL directly (that stays inside
app/execution/repository.py).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel

from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket, day_of_week_for_timestamp, time_bucket_for_minutes
from app.productivity.data_prep import Observation, Period, build_observations
from app.productivity.filters import ObservationFilters, apply_filters
from app.productivity.insights import Insight, generate_insights
from app.productivity.ml_prediction import MLDurationPrediction, predict_duration_with_ml
from app.productivity.prediction import DurationPrediction, predict_duration
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
    """Builds ProductivityReport and DurationPrediction results from execution history."""

    def __init__(
        self,
        repository: ExecutionRepository,
        thresholds: ProductivityThresholds = ProductivityThresholds(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._thresholds = thresholds
        self._clock = clock

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
