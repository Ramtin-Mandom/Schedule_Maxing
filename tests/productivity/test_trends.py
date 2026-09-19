"""Tests for app/productivity/trends.py: recent-vs-baseline comparison."""

from __future__ import annotations

import pytest

from app.execution.models import ExecutionStatus
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.stats import EvidenceLevel, ProductivityThresholds
from app.productivity.trends import compute_recent_trend

TIMESTAMP = "2024-01-01T09:00:00+00:00"
THRESHOLDS = ProductivityThresholds(low=2, moderate=4, high=6)


def _completed(actual_minutes: float, planned_duration: int = 60) -> Observation:
    return Observation(
        execution_id="e", task_name="task", category="study", tag="tag", priority=5,
        status=ExecutionStatus.COMPLETED, planned_date=1, planned_start=0, planned_end=planned_duration,
        planned_duration=planned_duration, created_at=TIMESTAMP, time_bucket=TimeBucket.MORNING,
        day_of_week="Monday", actual_active_duration_minutes=actual_minutes,
        duration_variance_minutes=actual_minutes - planned_duration, start_delay_minutes=0.0,
        focus_rating=None, energy_rating=None, interruption_count=None,
        is_completed=True, is_skipped=False, is_terminal=True, is_duration_plausible=True,
    )


def _skipped() -> Observation:
    return Observation(
        execution_id="e", task_name="task", category="study", tag="tag", priority=5,
        status=ExecutionStatus.SKIPPED, planned_date=1, planned_start=0, planned_end=60,
        planned_duration=60, created_at=TIMESTAMP, time_bucket=TimeBucket.MORNING,
        day_of_week="Monday", actual_active_duration_minutes=None, duration_variance_minutes=None,
        start_delay_minutes=None, focus_rating=None, energy_rating=None, interruption_count=None,
        is_completed=False, is_skipped=True, is_terminal=True, is_duration_plausible=True,
    )


def test_recent_trend_compares_recent_and_baseline_independently() -> None:
    recent = [_completed(70), _completed(75)]  # both completed, no skips
    baseline = [_completed(60), _completed(60), _skipped(), _skipped()]  # 50% completion historically

    trend = compute_recent_trend(recent, baseline, THRESHOLDS)

    assert trend.recent_observation_count == 2
    assert trend.recent_completion_rate == pytest.approx(1.0)
    assert trend.baseline_observation_count == 4
    assert trend.baseline_completion_rate == pytest.approx(0.5)


def test_recent_trend_with_empty_recent_window_is_safe() -> None:
    baseline = [_completed(60), _completed(65)]

    trend = compute_recent_trend([], baseline, THRESHOLDS)

    assert trend.recent_observation_count == 0
    assert trend.recent_evidence_level == EvidenceLevel.INSUFFICIENT
    assert trend.recent_completion_rate is None  # not fabricated as 0 or 1
    assert trend.baseline_observation_count == 2


def test_recent_trend_with_both_empty_is_safe() -> None:
    trend = compute_recent_trend([], [], THRESHOLDS)

    assert trend.recent_observation_count == 0
    assert trend.baseline_observation_count == 0
    assert trend.recent_completion_rate is None
    assert trend.baseline_completion_rate is None


def test_recent_trend_duration_variance_direction() -> None:
    recent = [_completed(80, planned_duration=60)]  # +20 minutes over
    baseline = [_completed(65, planned_duration=60)]  # +5 minutes over

    trend = compute_recent_trend(recent, baseline, THRESHOLDS)

    assert trend.recent_median_duration_variance_minutes == pytest.approx(20.0)
    assert trend.baseline_median_duration_variance_minutes == pytest.approx(5.0)
