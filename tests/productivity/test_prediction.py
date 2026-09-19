"""Tests for app/productivity/prediction.py: every fallback level individually
forced, outlier/implausible-value exclusion, a robustness comparison against a
naive mean baseline, and order independence."""

from __future__ import annotations

import random
import statistics

import pytest

from app.execution.models import ExecutionStatus
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.prediction import FallbackLevel, predict_duration
from app.productivity.stats import EvidenceLevel, ProductivityThresholds

THRESHOLDS = ProductivityThresholds(low=3, moderate=6, high=10)
TIMESTAMP = "2024-01-01T09:00:00+00:00"


def _completed(
    category: str,
    time_bucket: TimeBucket,
    actual_minutes: float,
    *,
    planned_duration: int = 60,
    plausible: bool = True,
) -> Observation:
    return Observation(
        execution_id="e",
        task_name="task",
        category=category,
        tag="tag",
        priority=5,
        status=ExecutionStatus.COMPLETED,
        planned_date=1,
        planned_start=0,
        planned_end=planned_duration,
        planned_duration=planned_duration,
        created_at=TIMESTAMP,
        time_bucket=time_bucket,
        day_of_week="Monday",
        actual_active_duration_minutes=actual_minutes,
        duration_variance_minutes=actual_minutes - planned_duration,
        start_delay_minutes=0.0,
        focus_rating=None,
        energy_rating=None,
        interruption_count=None,
        is_completed=True,
        is_skipped=False,
        is_terminal=True,
        is_duration_plausible=plausible,
    )


def test_level_1_category_and_time_bucket_match() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 58),
        _completed("study", TimeBucket.MORNING, 60),
        _completed("study", TimeBucket.MORNING, 62),
        _completed("exercise", TimeBucket.EVENING, 40),
    ]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.CATEGORY_TIME_BUCKET
    assert prediction.sample_count == 3
    assert prediction.predicted_duration_minutes == pytest.approx(60.0)


def test_level_2_falls_back_to_category_only() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 59),  # only 1 morning -> insufficient
        _completed("study", TimeBucket.AFTERNOON, 60),
        _completed("study", TimeBucket.AFTERNOON, 65),
        _completed("study", TimeBucket.AFTERNOON, 70),
    ]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.CATEGORY
    assert prediction.sample_count == 4
    assert prediction.predicted_duration_minutes == pytest.approx(statistics.median([59, 60, 65, 70]))


def test_level_3_falls_back_to_time_bucket_only() -> None:
    observations = [
        _completed("study", TimeBucket.EVENING, 40),  # study has data, but not in morning
        _completed("study", TimeBucket.EVENING, 42),
        _completed("exercise", TimeBucket.MORNING, 20),
        _completed("errand", TimeBucket.MORNING, 25),
        _completed("errand", TimeBucket.MORNING, 30),
    ]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.TIME_BUCKET
    assert prediction.sample_count == 3  # the 3 morning observations, any category


def test_level_4_falls_back_to_global() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 60),  # only 1 study/morning
        _completed("exercise", TimeBucket.AFTERNOON, 30),
        _completed("errand", TimeBucket.EVENING, 20),
    ]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.GLOBAL
    assert prediction.sample_count == 3


def test_level_5_falls_back_to_original_estimate_when_history_is_too_thin() -> None:
    observations = [_completed("study", TimeBucket.MORNING, 60)]  # only 1 observation total

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.ORIGINAL_ESTIMATE
    assert prediction.evidence_level == EvidenceLevel.INSUFFICIENT
    assert prediction.predicted_duration_minutes == pytest.approx(45.0)
    assert prediction.original_estimate_minutes == pytest.approx(45.0)


def test_empty_history_falls_back_to_original_estimate() -> None:
    prediction = predict_duration(
        [], category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.fallback_level == FallbackLevel.ORIGINAL_ESTIMATE
    assert prediction.sample_count == 0
    assert prediction.predicted_duration_minutes == pytest.approx(45.0)


def test_implausible_outlier_excluded_from_prediction_basis() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 58),
        _completed("study", TimeBucket.MORNING, 60),
        _completed("study", TimeBucket.MORNING, 62),
        _completed("study", TimeBucket.MORNING, 2000, plausible=False),  # corrupted/implausible
    ]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert prediction.sample_count == 3  # the implausible row is excluded, not just down-weighted
    assert prediction.predicted_duration_minutes == pytest.approx(60.0)


def test_median_prediction_is_more_robust_than_a_naive_mean_baseline() -> None:
    # True cluster center is ~60; one plausible but atypical outlier at 300.
    actual_durations = [58, 59, 60, 61, 300]
    observations = [_completed("study", TimeBucket.MORNING, value) for value in actual_durations]

    prediction = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )
    naive_mean_baseline = statistics.mean(actual_durations)

    assert prediction.predicted_duration_minutes == pytest.approx(60.0)
    assert naive_mean_baseline == pytest.approx(107.6)
    # Our median-based prediction stays close to the true center; the naive mean does not.
    assert abs(prediction.predicted_duration_minutes - 60.0) < abs(naive_mean_baseline - 60.0)


def test_prediction_is_independent_of_observation_order() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 58),
        _completed("study", TimeBucket.MORNING, 60),
        _completed("study", TimeBucket.MORNING, 62),
        _completed("exercise", TimeBucket.EVENING, 40),
    ]

    baseline = predict_duration(
        observations, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    shuffled = observations[:]
    random.Random(7).shuffle(shuffled)
    reordered = predict_duration(
        shuffled, category="study", time_bucket=TimeBucket.MORNING,
        original_estimate_minutes=45, thresholds=THRESHOLDS,
    )

    assert baseline == reordered
