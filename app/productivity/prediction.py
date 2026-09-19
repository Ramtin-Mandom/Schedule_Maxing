"""
prediction.py

A simple, explainable duration estimator built on robust historical
statistics (the median of completed durations), not a model. It never
writes back into the optimizer or any Task/TaskExecution -- callers decide
whether and how to use the returned prediction; the caller's own estimate is
always echoed back unchanged.

Fallback hierarchy (documented, walked in this exact order):
    1. category + time bucket match
    2. category match (any time bucket)
    3. time bucket match (any category)
    4. global history (all completed tasks)
    5. the caller's own original estimate, when even global history is too thin

A level is used once its sample count meets thresholds.low; otherwise
prediction falls through to the next level. The final level always succeeds
(it has no sample-size requirement), so predict_duration never raises for
lack of data -- it just reports FallbackLevel.ORIGINAL_ESTIMATE with
EvidenceLevel.INSUFFICIENT.

Outlier handling: the prediction basis at every level only includes
completed observations with a *plausible* actual duration (see
app/productivity/data_prep.py's is_duration_plausible /
MAX_PLAUSIBLE_DURATION_MINUTES), and uses the median rather than the mean,
so a single corrupted or extreme row cannot dominate the estimate.
"""

from __future__ import annotations

import statistics
from enum import Enum

from pydantic import BaseModel

from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.stats import EvidenceLevel, ProductivityThresholds, evidence_level_for_count


class FallbackLevel(str, Enum):
    CATEGORY_TIME_BUCKET = "category_time_bucket"
    CATEGORY = "category"
    TIME_BUCKET = "time_bucket"
    GLOBAL = "global"
    ORIGINAL_ESTIMATE = "original_estimate"


class DurationPrediction(BaseModel):
    predicted_duration_minutes: float
    original_estimate_minutes: float
    sample_count: int
    fallback_level: FallbackLevel
    evidence_level: EvidenceLevel
    explanation: str


def predict_duration(
    observations: list[Observation],
    *,
    category: str,
    time_bucket: TimeBucket,
    original_estimate_minutes: float,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> DurationPrediction:
    """Predict a task's actual duration using the documented fallback hierarchy above."""
    basis = [
        observation
        for observation in observations
        if observation.is_completed
        and observation.actual_active_duration_minutes is not None
        and observation.is_duration_plausible
    ]

    candidates: list[tuple[FallbackLevel, list[Observation]]] = [
        (
            FallbackLevel.CATEGORY_TIME_BUCKET,
            [o for o in basis if o.category == category and o.time_bucket == time_bucket],
        ),
        (FallbackLevel.CATEGORY, [o for o in basis if o.category == category]),
        (FallbackLevel.TIME_BUCKET, [o for o in basis if o.time_bucket == time_bucket]),
        (FallbackLevel.GLOBAL, basis),
    ]

    for level, subset in candidates:
        if len(subset) >= thresholds.low:
            durations = [o.actual_active_duration_minutes for o in subset]
            predicted = round(statistics.median(durations), 2)
            return DurationPrediction(
                predicted_duration_minutes=predicted,
                original_estimate_minutes=original_estimate_minutes,
                sample_count=len(subset),
                fallback_level=level,
                evidence_level=evidence_level_for_count(len(subset), thresholds),
                explanation=_explanation(level, len(subset), category, time_bucket, predicted),
            )

    return DurationPrediction(
        predicted_duration_minutes=original_estimate_minutes,
        original_estimate_minutes=original_estimate_minutes,
        sample_count=len(basis),
        fallback_level=FallbackLevel.ORIGINAL_ESTIMATE,
        evidence_level=EvidenceLevel.INSUFFICIENT,
        explanation=(
            f"Not enough matching history ({len(basis)} completed task(s) with a plausible "
            f"duration found in total) to estimate '{category}' tasks in the {time_bucket.value} "
            f"time bucket; using your original estimate of {original_estimate_minutes:g} minutes unchanged."
        ),
    )


def _explanation(
    level: FallbackLevel,
    sample_count: int,
    category: str,
    time_bucket: TimeBucket,
    predicted_duration_minutes: float,
) -> str:
    if level is FallbackLevel.CATEGORY_TIME_BUCKET:
        return (
            f"Based on the median actual duration of {sample_count} completed '{category}' "
            f"task(s) in the {time_bucket.value} time bucket: {predicted_duration_minutes:g} minutes."
        )
    if level is FallbackLevel.CATEGORY:
        return (
            f"Not enough '{category}' history specifically in the {time_bucket.value} time bucket; "
            f"based on the median actual duration of {sample_count} completed '{category}' task(s) "
            f"across all time buckets: {predicted_duration_minutes:g} minutes."
        )
    if level is FallbackLevel.TIME_BUCKET:
        return (
            f"Not enough '{category}' history; based on the median actual duration of "
            f"{sample_count} completed task(s) in the {time_bucket.value} time bucket across all "
            f"categories: {predicted_duration_minutes:g} minutes."
        )
    return (
        f"Not enough category- or time-bucket-specific history; based on the median actual "
        f"duration of {sample_count} completed task(s) across all history: "
        f"{predicted_duration_minutes:g} minutes."
    )
