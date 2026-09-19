"""
trends.py

A simple "recent vs. baseline" comparison, e.g. for a Productivity page's
"recent trends" panel. Deliberately minimal: two numbers (completion rate,
median duration variance) compared between a recent window and a longer
baseline, each carrying its own sample count and evidence level so a caller
never presents a thin recent window as a confident trend.

This is not a time-series/forecasting feature -- it's one before/after
comparison, consistent with this milestone's "no predictions/ML beyond the
documented median-based estimator" scope.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.productivity.data_prep import Observation
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, compute_segment_stats


class RecentTrend(BaseModel):
    recent_observation_count: int
    recent_evidence_level: EvidenceLevel
    recent_completion_rate: float | None
    recent_median_duration_variance_minutes: float | None

    baseline_observation_count: int
    baseline_evidence_level: EvidenceLevel
    baseline_completion_rate: float | None
    baseline_median_duration_variance_minutes: float | None


def compute_recent_trend(
    recent_observations: list[Observation],
    baseline_observations: list[Observation],
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> RecentTrend:
    """
    Compare a recent window of observations against a baseline (typically
    all-time or a longer window). Both inputs are plain Observation lists --
    callers build them however suits them (e.g. one `days=7` call and one
    `days=None` call to app.productivity.data_prep.build_observations).
    """
    recent_stats = compute_segment_stats(recent_observations, thresholds)
    baseline_stats = compute_segment_stats(baseline_observations, thresholds)

    return RecentTrend(
        recent_observation_count=recent_stats.observation_count,
        recent_evidence_level=recent_stats.evidence_level,
        recent_completion_rate=recent_stats.completion_rate,
        recent_median_duration_variance_minutes=recent_stats.median_duration_variance_minutes,
        baseline_observation_count=baseline_stats.observation_count,
        baseline_evidence_level=baseline_stats.evidence_level,
        baseline_completion_rate=baseline_stats.completion_rate,
        baseline_median_duration_variance_minutes=baseline_stats.median_duration_variance_minutes,
    )
