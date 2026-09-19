"""
segments.py

Groups Observation records the ways required by Milestone 2: globally, and
by category, tag, day of week, time bucket, and category-combined-with-time-
bucket. Each grouping returns a dict keyed by a deterministic, sorted key
(built from `sorted(set(...))`, never from dict/list insertion order), so
results never depend on the order rows happened to come back from the
database.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable

from app.productivity.data_prep import Observation
from app.productivity.stats import ProductivityThresholds, SegmentStats, compute_segment_stats


def global_stats(observations: list[Observation], thresholds: ProductivityThresholds) -> SegmentStats:
    """Statistics across the entire given observation set, ungrouped."""
    return compute_segment_stats(observations, thresholds)


def by_category(
    observations: list[Observation], thresholds: ProductivityThresholds
) -> dict[str, SegmentStats]:
    return _group_by(observations, thresholds, key_fn=lambda observation: observation.category)


def by_tag(observations: list[Observation], thresholds: ProductivityThresholds) -> dict[str, SegmentStats]:
    return _group_by(observations, thresholds, key_fn=lambda observation: observation.tag)


def by_day_of_week(
    observations: list[Observation], thresholds: ProductivityThresholds
) -> dict[str, SegmentStats]:
    return _group_by(observations, thresholds, key_fn=lambda observation: observation.day_of_week)


def by_time_bucket(
    observations: list[Observation], thresholds: ProductivityThresholds
) -> dict[str, SegmentStats]:
    return _group_by(observations, thresholds, key_fn=lambda observation: observation.time_bucket.value)


def by_category_and_time_bucket(
    observations: list[Observation], thresholds: ProductivityThresholds
) -> dict[tuple[str, str], SegmentStats]:
    return _group_by(
        observations,
        thresholds,
        key_fn=lambda observation: (observation.category, observation.time_bucket.value),
    )


def best_supported_time_bucket_by_category(
    by_category_and_time_bucket: dict[tuple[str, str], SegmentStats],
) -> dict[str, tuple[str, SegmentStats]]:
    """
    For each category, the time bucket with the most evidence for duration
    accuracy -- the largest completed_duration_count among that category's
    time-bucket segments -- paired with its stats. Ties are broken by sorted
    time-bucket name for determinism.

    "Most evidence" is not the same as "best outcome": this picks the bucket
    with the most completed, duration-bearing observations, not the one with
    the highest completion rate or shortest duration. A caller should still
    check the returned SegmentStats' evidence_level/completed_duration_count
    before presenting it as a confident recommendation -- a category with no
    completed observations in any bucket still returns its least-thin
    bucket, which may itself be EvidenceLevel.INSUFFICIENT.
    """
    categories = sorted({category for category, _time_bucket in by_category_and_time_bucket})
    result: dict[str, tuple[str, SegmentStats]] = {}

    for category in categories:
        candidates = sorted(
            (
                (time_bucket, stats)
                for (candidate_category, time_bucket), stats in by_category_and_time_bucket.items()
                if candidate_category == category
            ),
            key=lambda item: (-item[1].completed_duration_count, item[0]),
        )
        result[category] = candidates[0]

    return result


def _group_by(
    observations: list[Observation],
    thresholds: ProductivityThresholds,
    *,
    key_fn: Callable[[Observation], Hashable],
) -> dict:
    keys = sorted({key_fn(observation) for observation in observations})
    return {
        key: compute_segment_stats(
            [observation for observation in observations if key_fn(observation) == key],
            thresholds,
        )
        for key in keys
    }
