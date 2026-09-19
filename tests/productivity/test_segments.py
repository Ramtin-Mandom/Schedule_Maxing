"""Tests for app/productivity/segments.py: each required grouping against the
synthetic fixture dataset."""

from __future__ import annotations

from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation, build_observations
from app.productivity.segments import (
    best_supported_time_bucket_by_category,
    by_category,
    by_category_and_time_bucket,
    by_day_of_week,
    by_tag,
    by_time_bucket,
    global_stats,
)
from app.productivity.stats import ProductivityThresholds

THRESHOLDS = ProductivityThresholds(low=3, moderate=6, high=10)
TIMESTAMP = "2024-01-01T09:00:00+00:00"


def _completed(category: str, time_bucket: TimeBucket, actual_minutes: float) -> Observation:
    return Observation(
        execution_id="e", task_name="task", category=category, tag="tag", priority=5,
        status=ExecutionStatus.COMPLETED, planned_date=1, planned_start=0, planned_end=60,
        planned_duration=60, created_at=TIMESTAMP, time_bucket=time_bucket, day_of_week="Monday",
        actual_active_duration_minutes=actual_minutes, duration_variance_minutes=actual_minutes - 60,
        start_delay_minutes=0.0, focus_rating=None, energy_rating=None, interruption_count=None,
        is_completed=True, is_skipped=False, is_terminal=True, is_duration_plausible=True,
    )


def test_global_stats_covers_every_observation(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    stats = global_stats(observations, THRESHOLDS)

    assert stats.observation_count == len(observations)


def test_by_category_has_expected_keys(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    segments = by_category(observations, THRESHOLDS)

    assert set(segments) == {"study", "exercise", "errand"}
    # study has the most observations across both time buckets + skips.
    assert segments["study"].observation_count > segments["errand"].observation_count


def test_by_tag_groups_distinct_tags(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    segments = by_tag(observations, THRESHOLDS)

    assert "math" in segments
    assert "cardio" in segments
    assert "car" in segments


def test_by_day_of_week_only_contains_real_weekday_names(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    segments = by_day_of_week(observations, THRESHOLDS)

    valid_names = {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    }
    assert set(segments).issubset(valid_names)
    assert len(segments) >= 1


def test_by_time_bucket_has_expected_keys(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    segments = by_time_bucket(observations, THRESHOLDS)

    assert set(segments) == {"morning", "evening", "afternoon"}


def test_by_category_and_time_bucket_is_more_specific_than_by_category(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    combined = by_category_and_time_bucket(observations, THRESHOLDS)
    category_only = by_category(observations, THRESHOLDS)

    assert ("study", "morning") in combined
    assert ("study", "evening") in combined
    # The combined segment is a strict subset of the category segment's observations.
    assert combined[("study", "morning")].observation_count < category_only["study"].observation_count


def test_best_supported_time_bucket_by_category_picks_most_evidence() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 58),
        _completed("study", TimeBucket.MORNING, 60),
        _completed("study", TimeBucket.MORNING, 62),
        _completed("study", TimeBucket.EVENING, 40),
        _completed("exercise", TimeBucket.EVENING, 30),
    ]
    combined = by_category_and_time_bucket(observations, THRESHOLDS)

    best = best_supported_time_bucket_by_category(combined)

    assert best["study"][0] == "morning"  # 3 morning observations beats 1 evening
    assert best["study"][1].completed_duration_count == 3
    assert best["exercise"][0] == "evening"


def test_best_supported_time_bucket_ties_broken_deterministically() -> None:
    observations = [
        _completed("study", TimeBucket.MORNING, 58),
        _completed("study", TimeBucket.EVENING, 40),
    ]
    combined = by_category_and_time_bucket(observations, THRESHOLDS)

    best = best_supported_time_bucket_by_category(combined)

    # Tied at 1 observation each -- "evening" sorts before "morning" alphabetically.
    assert best["study"][0] == "evening"


def test_segments_do_not_depend_on_observation_order(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    observations = build_observations(repository)

    forward = by_category(observations, THRESHOLDS)
    reversed_segments = by_category(list(reversed(observations)), THRESHOLDS)

    assert forward == reversed_segments
