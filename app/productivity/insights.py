"""
insights.py

Turns already-computed SegmentStats (see app/productivity/stats.py) into a
handful of structured, human-readable Insight records. Every insight is
built by formatting an f-string template around numbers that were already
computed elsewhere -- there is no hardcoded per-category text and no LLM
call.

A rule only fires once the specific metric it reports clears
EvidenceLevel.INSUFFICIENT -- evaluated against that metric's own sample
size (e.g. terminal_count for completion_rate, completed_duration_count for
median_duration_variance_minutes), not the segment's blended
observation_count-based evidence_level. A segment can hold many still-
pending (scheduled/in_progress/paused) records alongside only a few
terminal or completed ones, so using the blended count could overstate how
much evidence actually backs a given metric. This is what requirement
"never present a segment based on too little evidence as a confident
conclusion" means in practice here.

Insufficient-data insights are only produced for segments that exist (have
at least one observation) but fall short of the sample threshold. A
category/tag/bucket with literally zero observations never appears in a
grouped dict at all (see app/productivity/segments.py), so it is silently
absent from insights too -- there is nothing honest to say about data that
was never collected.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.productivity.stats import EvidenceLevel, ProductivityThresholds, SegmentStats, evidence_level_for_count


class Insight(BaseModel):
    text: str
    metric: str
    category: str | None
    time_bucket: str | None
    value: float | None
    sample_count: int
    evidence_level: EvidenceLevel


def generate_insights(
    *,
    by_category: dict[str, SegmentStats],
    by_category_and_time_bucket: dict[tuple[str, str], SegmentStats],
    thresholds: ProductivityThresholds,
) -> list[Insight]:
    """Generate every rule-based insight the current data supports, deterministically ordered."""
    insights: list[Insight] = []
    insights.extend(_completion_rate_insights(by_category_and_time_bucket, thresholds))
    insights.extend(_duration_variance_insights(by_category, thresholds))
    insights.extend(_insufficient_data_insights(by_category))

    return sorted(
        insights,
        key=lambda insight: (-insight.sample_count, insight.category or "", insight.time_bucket or ""),
    )


def _completion_rate_insights(
    by_category_and_time_bucket: dict[tuple[str, str], SegmentStats],
    thresholds: ProductivityThresholds,
) -> list[Insight]:
    insights: list[Insight] = []

    for (category, time_bucket), segment in by_category_and_time_bucket.items():
        if segment.completion_rate is None:
            continue

        # Gate on terminal_count's own evidence level, not the segment's blended
        # observation_count-based one: a segment can have many still-pending
        # (scheduled/in_progress/paused) records and very few terminal ones, in
        # which case completion_rate's real sample size is much smaller than it looks.
        metric_evidence = evidence_level_for_count(segment.terminal_count, thresholds)
        if metric_evidence == EvidenceLevel.INSUFFICIENT:
            continue

        insights.append(
            Insight(
                text=(
                    f"{category.capitalize()} tasks completed in the {time_bucket} have a "
                    f"completion rate of {segment.completion_rate:.0%} across {segment.terminal_count} "
                    f"observations."
                ),
                metric="completion_rate",
                category=category,
                time_bucket=time_bucket,
                value=segment.completion_rate,
                sample_count=segment.terminal_count,
                evidence_level=metric_evidence,
            )
        )

    return insights


def _duration_variance_insights(
    by_category: dict[str, SegmentStats],
    thresholds: ProductivityThresholds,
) -> list[Insight]:
    insights: list[Insight] = []

    for category, segment in by_category.items():
        if segment.median_duration_variance_minutes is None:
            continue

        # Gate on completed_duration_count's own evidence level -- this metric only
        # uses completed observations with a known actual duration, which can be a
        # much smaller pool than the category's total observation_count.
        metric_evidence = evidence_level_for_count(segment.completed_duration_count, thresholds)
        if metric_evidence == EvidenceLevel.INSUFFICIENT:
            continue

        variance = segment.median_duration_variance_minutes
        direction = "longer" if variance >= 0 else "shorter"

        insights.append(
            Insight(
                text=(
                    f"{category.capitalize()} tasks take a median of {abs(variance):g} minutes "
                    f"{direction} than estimated, based on {segment.completed_duration_count} observations."
                ),
                metric="median_duration_variance_minutes",
                category=category,
                time_bucket=None,
                value=variance,
                sample_count=segment.completed_duration_count,
                evidence_level=metric_evidence,
            )
        )

    return insights


def _insufficient_data_insights(by_category: dict[str, SegmentStats]) -> list[Insight]:
    insights: list[Insight] = []

    for category, segment in by_category.items():
        if segment.evidence_level != EvidenceLevel.INSUFFICIENT:
            continue

        insights.append(
            Insight(
                text=(
                    f"There is not enough history to recommend a time for {category} "
                    f"({segment.observation_count} observation(s) so far)."
                ),
                metric="insufficient_data",
                category=category,
                time_bucket=None,
                value=None,
                sample_count=segment.observation_count,
                evidence_level=segment.evidence_level,
            )
        )

    return insights
