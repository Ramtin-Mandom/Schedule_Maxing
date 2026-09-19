"""Tests for app/productivity/stats.py: every required metric, skipped-task
handling, missing-rating handling, evidence-level thresholds, and order
independence -- all against hand-built Observation lists (no database)."""

from __future__ import annotations

import random

import pytest

from app.execution.models import ExecutionStatus
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, compute_segment_stats, evidence_level_for_count

TIMESTAMP = "2024-01-01T09:00:00+00:00"  # a Monday


def _make_observation(**overrides: object) -> Observation:
    defaults: dict[str, object] = dict(
        execution_id="e",
        task_name="Study Math",
        category="study",
        tag="math",
        priority=8,
        status=ExecutionStatus.COMPLETED,
        planned_date=1,
        planned_start=540,
        planned_end=600,
        planned_duration=60,
        created_at=TIMESTAMP,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        actual_active_duration_minutes=60.0,
        duration_variance_minutes=0.0,
        start_delay_minutes=0.0,
        focus_rating=None,
        energy_rating=None,
        interruption_count=None,
        is_completed=True,
        is_skipped=False,
        is_terminal=True,
        is_duration_plausible=True,
    )
    defaults.update(overrides)
    return Observation(**defaults)


def _completed(actual_minutes: float, planned_duration: int = 60, **overrides: object) -> Observation:
    return _make_observation(
        actual_active_duration_minutes=actual_minutes,
        planned_duration=planned_duration,
        status=ExecutionStatus.COMPLETED,
        is_completed=True,
        is_skipped=False,
        is_terminal=True,
        **overrides,
    )


def _skipped(**overrides: object) -> Observation:
    return _make_observation(
        actual_active_duration_minutes=None,
        status=ExecutionStatus.SKIPPED,
        is_completed=False,
        is_skipped=True,
        is_terminal=True,
        **overrides,
    )


def _pending(status: ExecutionStatus = ExecutionStatus.SCHEDULED, **overrides: object) -> Observation:
    return _make_observation(
        actual_active_duration_minutes=None,
        status=status,
        is_completed=False,
        is_skipped=False,
        is_terminal=False,
        **overrides,
    )


# ----------------------------------------------------------------------
# Skipped tasks: affect completion/skip rate, never look like a 0-min completion
# ----------------------------------------------------------------------


def test_skipped_tasks_affect_completion_and_skip_rate() -> None:
    observations = [
        _completed(50),
        _completed(60),
        _completed(70),
        _skipped(),
        _skipped(),
    ]

    stats = compute_segment_stats(observations)

    assert stats.completion_rate == pytest.approx(3 / 5)
    assert stats.skip_rate == pytest.approx(2 / 5)


def test_skipped_tasks_excluded_from_duration_statistics() -> None:
    observations = [_completed(50), _completed(60), _completed(70), _skipped(), _skipped()]

    stats = compute_segment_stats(observations)

    # Median of [50, 60, 70] is 60 -- skipped tasks must not pull this toward 0.
    assert stats.median_actual_duration_minutes == pytest.approx(60.0)
    assert stats.productive_active_minutes == pytest.approx(180.0)


def test_pending_executions_excluded_from_completion_and_skip_rate() -> None:
    observations = [_completed(60), _skipped(), _pending(), _pending(ExecutionStatus.IN_PROGRESS)]

    stats = compute_segment_stats(observations)

    # Only the two terminal observations (1 completed, 1 skipped) count.
    assert stats.completion_rate == pytest.approx(0.5)
    assert stats.skip_rate == pytest.approx(0.5)


def test_all_pending_yields_none_rates_not_zero() -> None:
    observations = [_pending(), _pending(ExecutionStatus.PAUSED)]

    stats = compute_segment_stats(observations)

    assert stats.completion_rate is None
    assert stats.skip_rate is None
    assert stats.productive_active_minutes == 0.0  # a true sum over nothing, not a fabricated average


# ----------------------------------------------------------------------
# Missing ratings: None, never fabricated as 0
# ----------------------------------------------------------------------


def test_no_ratings_present_yields_none_not_zero() -> None:
    observations = [_completed(60, focus_rating=None, energy_rating=None)]

    stats = compute_segment_stats(observations)

    assert stats.avg_focus_rating is None
    assert stats.avg_energy_rating is None


def test_partial_ratings_average_only_present_values() -> None:
    observations = [
        _completed(60, focus_rating=4, energy_rating=None),
        _completed(60, focus_rating=2, energy_rating=5),
        _completed(60, focus_rating=None, energy_rating=None),
    ]

    stats = compute_segment_stats(observations)

    assert stats.avg_focus_rating == pytest.approx((4 + 2) / 2)
    assert stats.avg_energy_rating == pytest.approx(5.0)


# ----------------------------------------------------------------------
# Duration accuracy metrics
# ----------------------------------------------------------------------


def test_duration_mae_and_variance_and_ratio() -> None:
    # planned=60: actuals 70 (+10), 50 (-10), 78 (+18)
    observations = [
        _completed(70, planned_duration=60),
        _completed(50, planned_duration=60),
        _completed(78, planned_duration=60),
    ]

    stats = compute_segment_stats(observations)

    # compute_segment_stats rounds to 2 decimal places for readability.
    assert stats.duration_mae_minutes == pytest.approx((10 + 10 + 18) / 3, abs=0.01)
    assert stats.median_duration_variance_minutes == pytest.approx(10.0)  # median of [10, -10, 18]
    assert stats.median_actual_to_planned_ratio == pytest.approx(70 / 60, rel=1e-3)  # median ratio


def test_median_planned_duration_matches_completed_basis() -> None:
    # Two segments with different planned durations for their completed tasks;
    # a pending (non-terminal) observation's planned_duration must not be counted.
    observations = [
        _completed(70, planned_duration=60),
        _completed(50, planned_duration=90),
        _pending(planned_duration=999),
    ]

    stats = compute_segment_stats(observations)

    assert stats.median_planned_duration_minutes == pytest.approx(75.0)  # median of [60, 90]


def test_median_planned_duration_is_none_with_no_completed_observations() -> None:
    stats = compute_segment_stats([_skipped(), _pending()])
    assert stats.median_planned_duration_minutes is None


# ----------------------------------------------------------------------
# Start delay / on-schedule rate
# ----------------------------------------------------------------------


def test_on_schedule_start_rate_and_median_delay() -> None:
    thresholds = ProductivityThresholds(on_schedule_tolerance_minutes=10)
    observations = [
        _completed(60, start_delay_minutes=2),  # on schedule
        _completed(60, start_delay_minutes=-5),  # on schedule
        _completed(60, start_delay_minutes=30),  # late
    ]

    stats = compute_segment_stats(observations, thresholds)

    # compute_segment_stats rounds to 4 decimal places for readability.
    assert stats.on_schedule_start_rate == pytest.approx(2 / 3, abs=0.0001)
    assert stats.median_start_delay_minutes == pytest.approx(2.0)


def test_missing_start_delay_excluded_from_on_schedule_rate() -> None:
    observations = [
        _completed(60, start_delay_minutes=0),
        _completed(60, start_delay_minutes=None),
    ]

    stats = compute_segment_stats(observations)

    assert stats.on_schedule_start_rate == pytest.approx(1.0)  # only the known delay counts


# ----------------------------------------------------------------------
# Evidence levels
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, EvidenceLevel.INSUFFICIENT),
        (4, EvidenceLevel.INSUFFICIENT),
        (5, EvidenceLevel.LOW),
        (14, EvidenceLevel.LOW),
        (15, EvidenceLevel.MODERATE),
        (29, EvidenceLevel.MODERATE),
        (30, EvidenceLevel.HIGH),
        (100, EvidenceLevel.HIGH),
    ],
)
def test_evidence_level_thresholds(count: int, expected: EvidenceLevel) -> None:
    assert evidence_level_for_count(count, ProductivityThresholds()) == expected


def test_evidence_level_uses_configurable_thresholds() -> None:
    thresholds = ProductivityThresholds(low=2, moderate=4, high=6)
    assert evidence_level_for_count(1, thresholds) == EvidenceLevel.INSUFFICIENT
    assert evidence_level_for_count(2, thresholds) == EvidenceLevel.LOW
    assert evidence_level_for_count(4, thresholds) == EvidenceLevel.MODERATE
    assert evidence_level_for_count(6, thresholds) == EvidenceLevel.HIGH


# ----------------------------------------------------------------------
# Order independence
# ----------------------------------------------------------------------


def test_stats_are_independent_of_observation_order() -> None:
    observations = [
        _completed(70, start_delay_minutes=5, focus_rating=4),
        _completed(50, start_delay_minutes=-3, focus_rating=2),
        _skipped(),
        _completed(65, start_delay_minutes=20, energy_rating=3),
        _pending(),
    ]

    baseline = compute_segment_stats(observations)

    shuffled = observations[:]
    random.Random(42).shuffle(shuffled)
    reordered_stats = compute_segment_stats(shuffled)

    assert baseline == reordered_stats
