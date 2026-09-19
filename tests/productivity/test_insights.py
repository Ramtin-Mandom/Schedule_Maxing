"""Tests for app/productivity/insights.py: each rule's output against known
SegmentStats inputs, the insufficient-evidence gate, and deterministic ordering."""

from __future__ import annotations

from app.productivity.insights import generate_insights
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, SegmentStats

THRESHOLDS = ProductivityThresholds()


def _make_segment(**overrides: object) -> SegmentStats:
    defaults: dict[str, object] = dict(
        observation_count=17,
        terminal_count=17,
        completed_duration_count=17,
        evidence_level=EvidenceLevel.MODERATE,
        completion_rate=0.82,
        skip_rate=0.18,
        on_schedule_start_rate=0.7,
        median_start_delay_minutes=5.0,
        duration_mae_minutes=12.0,
        median_actual_duration_minutes=65.0,
        median_planned_duration_minutes=60.0,
        median_actual_to_planned_ratio=1.08,
        median_duration_variance_minutes=5.0,
        avg_focus_rating=4.0,
        avg_energy_rating=3.5,
        productive_active_minutes=800.0,
    )
    defaults.update(overrides)
    return SegmentStats(**defaults)


def test_completion_rate_insight_text_and_values() -> None:
    segment = _make_segment(completion_rate=0.82, observation_count=17)
    insights = generate_insights(
        by_category={},
        by_category_and_time_bucket={("study", "morning"): segment},
        thresholds=THRESHOLDS,
    )

    assert len(insights) == 1
    insight = insights[0]
    assert insight.metric == "completion_rate"
    assert insight.category == "study"
    assert insight.time_bucket == "morning"
    assert insight.sample_count == 17
    assert insight.text == "Study tasks completed in the morning have a completion rate of 82% across 17 observations."


def test_completion_rate_insight_skipped_when_insufficient_evidence() -> None:
    # terminal_count (not observation_count) is what gates this insight now: a segment can
    # have a large observation_count made mostly of still-pending records.
    segment = _make_segment(observation_count=20, terminal_count=2)
    insights = generate_insights(
        by_category={},
        by_category_and_time_bucket={("errand", "afternoon"): segment},
        thresholds=THRESHOLDS,
    )

    assert not any(insight.metric == "completion_rate" for insight in insights)


def test_completion_rate_insight_uses_terminal_count_not_observation_count() -> None:
    # 17 total observations, but only 6 are terminal -- the insight must cite 6, and its
    # evidence level must reflect 6 samples (LOW here), not the blended 17-based MODERATE.
    segment = _make_segment(observation_count=17, terminal_count=6, completion_rate=0.5)
    insights = generate_insights(
        by_category={},
        by_category_and_time_bucket={("study", "morning"): segment},
        thresholds=THRESHOLDS,
    )

    assert len(insights) == 1
    assert insights[0].sample_count == 6
    assert insights[0].evidence_level == EvidenceLevel.LOW
    assert "across 6 observations" in insights[0].text


def test_duration_variance_insight_longer_than_estimated() -> None:
    segment = _make_segment(median_duration_variance_minutes=18.0, completed_duration_count=20)
    insights = generate_insights(
        by_category={"exercise": segment},
        by_category_and_time_bucket={},
        thresholds=THRESHOLDS,
    )

    assert len(insights) == 1
    assert insights[0].metric == "median_duration_variance_minutes"
    assert insights[0].sample_count == 20
    assert "Exercise tasks take a median of 18 minutes longer than estimated" in insights[0].text


def test_duration_variance_insight_shorter_than_estimated() -> None:
    segment = _make_segment(median_duration_variance_minutes=-12.0, completed_duration_count=20)
    insights = generate_insights(
        by_category={"errand": segment},
        by_category_and_time_bucket={},
        thresholds=THRESHOLDS,
    )

    assert "shorter than estimated" in insights[0].text
    assert "12 minutes" in insights[0].text


def test_duration_variance_insight_gated_by_completed_duration_count() -> None:
    # Large observation_count, but very few of those are completed with a known duration.
    segment = _make_segment(
        median_duration_variance_minutes=18.0, observation_count=50, completed_duration_count=3,
    )
    insights = generate_insights(
        by_category={"exercise": segment},
        by_category_and_time_bucket={},
        thresholds=THRESHOLDS,
    )

    assert insights == []


def test_insufficient_data_insight_text() -> None:
    segment = _make_segment(
        evidence_level=EvidenceLevel.INSUFFICIENT,
        observation_count=2,
        terminal_count=2,
        completed_duration_count=2,
        median_duration_variance_minutes=None,
    )
    insights = generate_insights(
        by_category={"errand": segment},
        by_category_and_time_bucket={},
        thresholds=THRESHOLDS,
    )

    assert len(insights) == 1
    assert insights[0].metric == "insufficient_data"
    assert "There is not enough history to recommend a time for errand" in insights[0].text


def test_insights_are_sorted_deterministically() -> None:
    small = _make_segment(completed_duration_count=6, median_duration_variance_minutes=3.0)
    large = _make_segment(completed_duration_count=40, median_duration_variance_minutes=3.0)

    insights = generate_insights(
        by_category={"a_category": small, "z_category": large},
        by_category_and_time_bucket={},
        thresholds=THRESHOLDS,
    )

    # Higher sample_count sorts first, regardless of category name / dict insertion order.
    assert [insight.category for insight in insights[:2]] == ["z_category", "a_category"]


def test_no_insight_for_categories_with_zero_observations() -> None:
    # A category that never appears in the grouped dicts (zero observations) must not be mentioned.
    insights = generate_insights(by_category={}, by_category_and_time_bucket={}, thresholds=THRESHOLDS)
    assert insights == []
